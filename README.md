# Adhan Player

Plays the Adhan (Islamic call to prayer) automatically at the right time, through
a Bluetooth speaker, on a Raspberry Pi. This repo is the single source of truth —
edit here, push, and the Pi updates itself within about 2 minutes. You should
never need to SSH into the Pi or edit files there directly.

## How it's organized

```
adhan_player.py   — the scheduler. Calculates prayer times, plays adhan.mp3
                     at the right moment, logs everything.
adhan.mp3          — the adhan audio.
silence.mp3        — optional few seconds of silence played before the adhan,
                     so the Bluetooth speaker is fully awake before it starts.
deploy/            — everything needed to run this unattended on the Pi.
```

### What's inside `deploy/`

| File | What it does |
|---|---|
| `git_sync.sh` | Runs on a timer. Pulls this repo, and if anything changed, re-installs the systemd files below and restarts the affected services. This is the whole "auto-update" mechanism. |
| `adhan-sync.service` / `.timer` | The systemd timer that runs `git_sync.sh` every 2 minutes. |
| `bt_volume_guard.sh` | Watches for the Bluetooth speaker (re)connecting and pins its volume — see below. |
| `bt-volume-guard.service` | Keeps `bt_volume_guard.sh` running in the background, restarting it if it ever dies. |
| `bluetooth-speaker-connect.service` | Connects the Bluetooth speaker automatically at boot. |

## How the auto-update works

1. You edit a file (e.g. `adhan_player.py`) on your laptop.
2. `git add`, `git commit`, `git push`.
3. On the Pi, a systemd timer runs `git_sync.sh` every 2 minutes.
4. It checks GitHub for new commits. If there are any, it pulls them, re-installs
   any changed service files, and restarts `adhan.service` (and
   `bt-volume-guard.service` if that one changed too).

No SSH, no manual copying, no logging into the Pi. The Pi only ever reaches
*out* to GitHub — nothing needs to reach *in* to the Pi, which is why this setup
works even though direct SSH to the Pi has been unreliable.

## Common tasks

### Change the Bluetooth speaker's volume

Edit the `TARGET_VOLUME` line near the top of `deploy/bt_volume_guard.sh`:

```bash
TARGET_VOLUME="40%"   # change this
```

Commit and push. Within 2 minutes the Pi will pick it up and the next time the
speaker (re)connects — or right away, since the guard also restarts — it'll be
pinned to the new volume.

### Change prayer time settings (location, calculation method)

Edit the `CONFIGURATION` block near the top of `adhan_player.py`
(`LATITUDE`, `LONGITUDE`, `METHOD`). Push as normal.

### Check everything is alive (on the Pi, if you ever need to)

```bash
systemctl --user status adhan.service          # the scheduler itself
systemctl --user status adhan-sync.timer        # the auto-updater
systemctl --user status bt-volume-guard.service # the volume fixer
journalctl --user -t adhan-sync -f              # watch updates land in real time
```

## Why the volume used to reset

Bluetooth sinks default to 100% volume every time they're created, and
power-cycling the speaker makes the Pi's audio system create a brand new sink —
it doesn't just "resume" the old one. `bt_volume_guard.sh` watches for that and
re-applies the volume you want, every single time, regardless of what caused
the reconnect (power-cycle, reboot, manual reconnect).

## First-time setup on a Pi

This only matters if you're ever setting this up on a fresh Pi from scratch.

```bash
git clone https://<READ_ONLY_TOKEN>@github.com/aalrefai7/azan_system.git ~/adhan-player
cd ~/adhan-player

mkdir -p ~/.config/systemd/user
cp deploy/adhan-sync.service deploy/adhan-sync.timer deploy/bt-volume-guard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now adhan-sync.timer
systemctl --user enable --now bt-volume-guard.service

python adhan_player.py --install     # generates and prints the adhan.service unit
systemctl --user daemon-reload
systemctl --user enable --now adhan.service

sudo loginctl enable-linger pi        # so services keep running after logout/reboot
```

The read-only token is scoped to just this repo (Contents: Read-only), created at
`github.com/settings/tokens?type=beta`. It only needs read access since the Pi
never pushes — it only pulls.

## Known limitations

- **`/etc/bluetooth/connect_speaker.sh` isn't in this repo.** It's the script
  that actually pairs/connects the speaker at boot (referenced by
  `bluetooth-speaker-connect.service`), but it lives outside `~/adhan-player`
  and was hand-edited directly on the Pi, so it was never captured here. If
  the Pi's SD card is ever rebuilt, that script has to be rewritten from
  scratch. Worth pulling its contents into this repo next time it's touched.
- **Anyone who can push to this GitHub repo can run arbitrary code on the Pi**
  within about 2 minutes — that's the nature of an auto-pull-and-run setup.
  Fine as long as the repo stays private with just you as a collaborator;
  don't add collaborators or make it public without rethinking this.
- **`git_sync.sh` clears a stale `.git/index.lock` older than 5 minutes** on
  every run, to recover automatically if the Pi loses power mid-sync (it's
  had under-voltage issues before). Safe here since nothing else ever writes
  to this repo — just worth knowing if you ever add something else that does.
