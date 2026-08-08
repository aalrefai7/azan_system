"""
adhan_player.py — Production-grade Adhan scheduler for Waterloo, ON
====================================================================
Improvements over original:
  • Structured rotating-file logging (survives restarts)
  • Precise sleep-until-next-prayer (no busy-polling drift)
  • One-sided trigger window: only fires AFTER prayer time (never before)
  • Threaded audio playback (scheduler never blocks)
  • Per-prayer played-guard via (date, name) key — crash-safe on restart
  • Pre-flight checks: files exist, mpg123 on PATH
  • Exponential back-off on repeated errors
  • Graceful SIGTERM / SIGINT shutdown
  • Systemd unit file printed on --install
  • Optional desktop notification (notify-send)
"""

import datetime
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from adhanpy.PrayerTimes import PrayerTimes
    from adhanpy.calculation import CalculationMethod
    from adhanpy.data.Coordinates import Coordinates
except ImportError:
    sys.exit("ERROR: adhanpy is not installed. Run: pip install adhanpy")

# ─── CONFIGURATION ────────────────────────────────────────────────────────────
LATITUDE     = 43.4643          # Waterloo, ON
LONGITUDE    = -80.5204
METHOD       = CalculationMethod.NORTH_AMERICA

BASE_DIR     = Path("~/adhan-player").expanduser()
ADHAN_FILE   = BASE_DIR / "adhan.mp3"
SILENCE_FILE = BASE_DIR / "silence.mp3"   # optional pre-roll silence

LOG_FILE     = BASE_DIR / "adhan.log"
LOG_MAX_BYTES   = 2 * 1024 * 1024   # 2 MB per log file
LOG_BACKUP_COUNT = 3

# Seconds after prayer time the trigger is still valid (one-sided window)
TRIGGER_WINDOW_SECS = 59

# How far ahead (seconds) to wake up before the next prayer for accuracy
WAKEUP_LEAD_SECS = 2

ENABLE_NOTIFY = True   # desktop notify-send popup
# ──────────────────────────────────────────────────────────────────────────────


# ─── LOGGING ──────────────────────────────────────────────────────────────────
def build_logger() -> logging.Logger:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("adhan")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Rotating file handler
    fh = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES,
                              backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = build_logger()


# ─── PREFLIGHT CHECKS ─────────────────────────────────────────────────────────
def preflight() -> bool:
    ok = True

    # Check mpg123
    result = subprocess.run(["which", "mpg123"], capture_output=True)
    if result.returncode != 0:
        log.error("mpg123 not found on PATH. Install with: sudo apt install mpg123")
        ok = False

    # Check audio files
    for label, path in [("adhan", ADHAN_FILE), ("silence", SILENCE_FILE)]:
        if not path.exists():
            if label == "silence":
                log.warning("Silence file not found (%s) — will skip pre-roll", path)
            else:
                log.error("Adhan file not found: %s", path)
                ok = False

    return ok


# ─── PRAYER TIMES ─────────────────────────────────────────────────────────────
def get_prayer_times(date: datetime.date) -> dict[str, datetime.datetime]:
    """
    Returns prayer times as naive local datetimes for the given date.
    Retries up to 3 times with back-off on library errors.
    """
    for attempt in range(1, 4):
        try:
            pt = PrayerTimes((LATITUDE, LONGITUDE), date, METHOD)
            times = {
                "Fajr":    pt.fajr.astimezone().replace(tzinfo=None),
                "Dhuhr":   pt.dhuhr.astimezone().replace(tzinfo=None),
                "Asr":     pt.asr.astimezone().replace(tzinfo=None),
                "Maghrib": pt.maghrib.astimezone().replace(tzinfo=None),
                "Isha":    pt.isha.astimezone().replace(tzinfo=None),
            }
            return times
        except Exception as exc:
            wait = 2 ** attempt
            log.warning("Prayer-time calculation failed (attempt %d/3): %s — "
                        "retrying in %ds", attempt, exc, wait)
            time.sleep(wait)
    raise RuntimeError("Could not calculate prayer times after 3 attempts.")


# ─── AUDIO PLAYBACK ───────────────────────────────────────────────────────────
_audio_lock = threading.Lock()   # prevent overlapping adhans

def play_adhan(prayer_name: str) -> None:
    """Plays adhan in a background thread; never blocks the scheduler."""
    def _run():
        if not _audio_lock.acquire(blocking=False):
            log.warning("Audio already playing — skipping %s adhan", prayer_name)
            return
        try:
            log.info("▶  Playing adhan for %s", prayer_name)
            _notify(f"🕌 {prayer_name}", "Time for prayer")
            _play_file(SILENCE_FILE, optional=True)
            _play_file(ADHAN_FILE, optional=False)
            log.info("✔  Adhan finished for %s", prayer_name)
        except Exception as exc:
            log.error("Audio playback error for %s: %s", prayer_name, exc)
        finally:
            _audio_lock.release()

    t = threading.Thread(target=_run, name=f"adhan-{prayer_name}", daemon=True)
    t.start()


def _play_file(path: Path, optional: bool) -> None:
    if not path.exists():
        if optional:
            return
        raise FileNotFoundError(f"Audio file missing: {path}")
    result = subprocess.run(
        ["mpg123", "--quiet", str(path)],
        timeout=600,          # 10-minute hard ceiling
        check=False,
    )
    if result.returncode not in (0, -15):   # -15 = SIGTERM (normal kill)
        log.warning("mpg123 exited with code %d for %s", result.returncode, path)


def _notify(summary: str, body: str) -> None:
    if not ENABLE_NOTIFY:
        return
    try:
        subprocess.run(
            ["notify-send", "--urgency=normal", "--expire-time=10000",
             summary, body],
            timeout=5, check=False,
        )
    except FileNotFoundError:
        pass   # notify-send not installed — silently skip


# ─── SCHEDULER ────────────────────────────────────────────────────────────────
class AdhanScheduler:
    def __init__(self):
        self._shutdown = threading.Event()
        self._prayers: dict[str, datetime.datetime] = {}
        self._current_date: datetime.date | None = None

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT,  self._handle_signal)

    # ── cross-process played guard ────────────────────────────────────────────
    def _played_flag_path(self, date: datetime.date, name: str) -> Path:
        return BASE_DIR / f".played_{date.isoformat()}_{name}"

    def _is_played(self, date: datetime.date, name: str) -> bool:
        return self._played_flag_path(date, name).exists()

    def _mark_played(self, date: datetime.date, name: str) -> bool:
        """Atomically create the flag file. Returns True only for the first caller."""
        flag = self._played_flag_path(date, name)
        try:
            flag.open("x").close()   # exclusive create — fails if already exists
            return True
        except FileExistsError:
            return False

    def _cleanup_old_flags(self, today: datetime.date) -> None:
        """Remove flag files older than today to avoid clutter."""
        for f in BASE_DIR.glob(".played_*"):
            try:
                date_str = f.name.split("_")[1]   # .played_YYYY-MM-DD_Name
                if datetime.date.fromisoformat(date_str) < today:
                    f.unlink(missing_ok=True)
            except (IndexError, ValueError):
                pass

    def _handle_signal(self, signum, _frame):
        log.info("Received signal %d — shutting down gracefully.", signum)
        self._shutdown.set()

    # ── date management ───────────────────────────────────────────────────────
    def _load_prayers_for_date(self, date: datetime.date) -> None:
        log.info("📅  Loading prayer times for %s",
                 date.strftime("%A %d %B %Y"))
        self._prayers = get_prayer_times(date)
        self._current_date = date
        self._cleanup_old_flags(date)
        for name, t in self._prayers.items():
            log.info("   %-8s %s", name, t.strftime("%H:%M:%S"))

    # ── next-prayer logic ─────────────────────────────────────────────────────
    def _next_wakeup(self, now: datetime.datetime) -> float:
        """
        Returns seconds to sleep until just before the next un-played prayer.
        Falls back to sleeping until midnight + 1 min if all prayers are past.
        """
        future = [
            t for name, t in self._prayers.items()
            if t > now and not self._is_played(self._current_date, name)
        ]
        if future:
            secs = (min(future) - now).total_seconds() - WAKEUP_LEAD_SECS
            return max(secs, 1.0)

        # All done today — sleep until start of tomorrow
        tomorrow = datetime.datetime.combine(
            now.date() + datetime.timedelta(days=1),
            datetime.time(0, 1)
        )
        return (tomorrow - now).total_seconds()

    # ── main loop ─────────────────────────────────────────────────────────────
    def run(self) -> None:
        log.info("✅  Adhan Player started  (PID %d)", os.getpid())
        consecutive_errors = 0

        while not self._shutdown.is_set():
            try:
                now = datetime.datetime.now()

                # Reload prayer times each new day
                if self._current_date != now.date():
                    self._load_prayers_for_date(now.date())

                # Check each prayer
                for name, prayer_time in self._prayers.items():
                    if self._is_played(self._current_date, name):
                        continue
                    elapsed = (now - prayer_time).total_seconds()
                    if 0 <= elapsed <= TRIGGER_WINDOW_SECS:
                        if not self._mark_played(self._current_date, name):
                            log.debug("⏭  %s already claimed by another instance — skipping", name)
                            continue
                        log.info("⏰  %s — firing adhan (%.1fs past time)",
                                 name, elapsed)
                        play_adhan(name)

                consecutive_errors = 0

                # Precision sleep until next prayer
                sleep_secs = self._next_wakeup(datetime.datetime.now())
                log.debug("💤  Sleeping %.1fs until next wakeup", sleep_secs)
                self._shutdown.wait(timeout=sleep_secs)

            except Exception as exc:
                consecutive_errors += 1
                wait = min(2 ** consecutive_errors, 300)  # cap at 5 minutes
                log.exception("Unexpected error (back-off %ds): %s", wait, exc)
                self._shutdown.wait(timeout=wait)

        log.info("👋  Adhan Player stopped.")


# ─── SYSTEMD UNIT INSTALLER ───────────────────────────────────────────────────
UNIT_TEMPLATE = """\
[Unit]
Description=Adhan Prayer Time Player
After=network.target sound.target

[Service]
Type=simple
ExecStart={python} {script}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus

[Install]
WantedBy=default.target
"""

def install_systemd_unit() -> None:
    unit = UNIT_TEMPLATE.format(
        python=sys.executable,
        script=Path(__file__).resolve(),
        uid=os.getuid(),
    )
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_file = unit_dir / "adhan.service"
    unit_file.write_text(unit)
    print(f"Systemd unit written to: {unit_file}")
    print("Enable and start with:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now adhan.service")
    print("View logs with:")
    print("  journalctl --user -u adhan.service -f")


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--install" in sys.argv:
        install_systemd_unit()
        sys.exit(0)

    if not preflight():
        sys.exit("Pre-flight checks failed. See log for details.")

    AdhanScheduler().run()
