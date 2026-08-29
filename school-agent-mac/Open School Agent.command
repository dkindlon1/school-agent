#!/usr/bin/env bash
# Double-click this in Finder to start School Agent.
#
# macOS opens .command files in Terminal from your HOME directory, not from
# where the file lives — hence the cd. Keep this file next to start.sh.
cd "$(dirname "$0")"
./start.sh
