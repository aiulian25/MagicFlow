#!/bin/sh
set -eu
fail() { echo "MagicFlow startup: $*" >&2; exit 1; }
case "${MAGICFLOW_FIX_OWNERSHIP:-0}" in 0|1) ;; *) fail 'MAGICFLOW_FIX_OWNERSHIP must be 0 or 1.' ;; esac
[ ! -L /data ] || fail '/data must be a real directory.'
mkdir -p /data || fail 'Cannot create /data; check mount permissions.'
if [ "$(id -u)" = 0 ] && [ "$(stat -c '%u:%g' /data)" != "$(id -u appuser):$(id -g appuser)" ]; then
    chown appuser:appuser /data || fail 'Cannot initialize /data ownership.'
fi
for path in /data/input /data/output /data/tmp /data/config; do
    [ ! -L "$path" ] || fail "$path must be a real directory."
    mkdir -p "$path" || fail "Cannot create $path; check mount permissions."
    if [ "$(id -u)" = 0 ]; then
        # Initialize mount roots only. Never rewrite an already-correct library.
        [ "$(stat -c '%u:%g' "$path")" = "$(id -u appuser):$(id -g appuser)" ] || chown appuser:appuser "$path"
        if [ "${MAGICFLOW_FIX_OWNERSHIP:-0}" = 1 ]; then
            # Explicit one-time migration; do not follow links or cross mounts.
            find "$path" -xdev \( ! -user appuser -o ! -group appuser \) -exec chown -h appuser:appuser {} +
        fi
        setpriv --reuid=appuser --regid=appuser --init-groups -- test -w "$path" \
            || fail "$path is not writable by appuser; repair permissions before starting."
    else
        [ -w "$path" ] || fail "$path is not writable by uid $(id -u)."
    fi
done
if [ "$(id -u)" = 0 ]; then
    # exec gives the service PID 1 and direct graceful-shutdown signals.
    exec setpriv --reuid=appuser --regid=appuser --init-groups -- "$@"
fi
exec "$@"
