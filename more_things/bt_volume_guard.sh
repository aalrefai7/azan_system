#!/bin/bash
# bt_volume_guard.sh — pins the Bluetooth speaker's volume every time it
# (re)connects. Bluetooth sinks default to 100% on creation, and bluetoothd's
# auto-reconnect (after a power-cycle) never goes through connect_speaker.sh,
# so nothing else re-applies the desired volume. This listens for every new
# sink and fixes the volume immediately, regardless of what triggered it.

TARGET_VOLUME="30%"   # adjust to taste

set_bt_volume() {
    local sink
    sink=$(pactl list short sinks | awk '/bluez/ {print $2; exit}')
    if [ -n "$sink" ]; then
        pactl set-default-sink "$sink"
        pactl set-sink-volume "$sink" "$TARGET_VOLUME"
        logger -t bt_volume_guard "set $sink to $TARGET_VOLUME"
    fi
}

# Catch the case where the speaker is already connected when this starts.
set_bt_volume

# Then react to every subsequent (re)connect.
pactl subscribe 2>/dev/null | while read -r line; do
    if [[ "$line" == *"'new' on sink"* ]]; then
        sleep 1   # let the sink fully register before touching it
        set_bt_volume
    fi
done
