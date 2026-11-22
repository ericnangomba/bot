#!/bin/sh
set -eu
mkdir -m 700 /tmp/workspace
cd /tmp/workspace
# Consume an archive of regular project files. No host paths are mounted.
tar --extract --file=- --no-same-owner --no-same-permissions
exec "$@" </dev/null
