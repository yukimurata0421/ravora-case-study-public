#!/bin/sh
set -eu

case "${1:-}" in
    youtube) mode=--auto-broadcast; port=8790 ;;
    twitch) mode=--publish-twitch; port=8793 ;;
    *) echo 'Usage: ray-studio-service.sh youtube|twitch' >&2; exit 2 ;;
esac
destination=$1
runtime="${XDG_RUNTIME_DIR:?}/ray-studio-$destination"
set -- --mode cloud "$mode" --until-stopped --port "$port" --operator-url-link "$runtime/operator.url"
if [ "$destination" = youtube ]; then
    set -- "$@" --privacy unlisted
    export YOUTUBE_READ_ENABLED=true
fi
if [ -f studio.toml ]; then
    set -- "$@" --config studio.toml
fi
if [ "${RAY_STUDIO_HOST_MIC:-browser}" = dell ]; then
    set -- "$@" --dell-mic
fi
export RAVORA_API_ENABLED=true
exec .venv/bin/ravora-youtube-live "$@"
