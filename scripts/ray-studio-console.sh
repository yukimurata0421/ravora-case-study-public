#!/bin/sh
# Print a single private URL to an authenticated SSH caller.
set -eu
case "${1:-}" in
    youtube) port=8790 ;;
    twitch) port=8793 ;;
    *) echo 'Usage: ray-studio-console.sh youtube|twitch' >&2; exit 2 ;;
esac
unit="ray-studio@$1.service"
url_file="${XDG_RUNTIME_DIR:?}/ray-studio-$1/operator.url"
state() { systemctl --user show "$unit" -p ActiveState --value; }
case "$(state)" in
    active|activating|reloading) ;;
    inactive|failed) systemctl --user start "$unit" >&2 ;;
    *) echo 'Ray is changing state. Try again after shutdown.' >&2; exit 1 ;;
esac

elapsed=0
while [ "$elapsed" -lt 120 ]; do
    case "$(state)" in
        active|activating|reloading) ;;
        *) echo "Ray startup stopped. Check the user journal for $unit." >&2; exit 1 ;;
    esac
    if [ -L "$url_file" ] && [ -s "$url_file" ] && IFS= read -r url < "$url_file"; then
        if ! printf '%s\n' "$url" | grep -Eq "^http://127\.0\.0\.1:$port/#[A-Za-z0-9_-]{43}$"; then
            echo 'Ray returned an invalid operator endpoint.' >&2
            exit 1
        fi
        printf '%s\n' "$url"
        exit 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done
echo "Timed out waiting for the operator screen. Check the user journal for $unit." >&2
exit 1
