#!/bin/sh
# Fix ownership of bind-mounted data directories, then drop to appuser
chown -R appuser:appuser /data
exec runuser -u appuser -- "$@"
