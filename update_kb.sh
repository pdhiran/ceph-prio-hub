#!/bin/bash
# Compat wrapper — canonical command is ./update_index.sh
exec "$(cd "$(dirname "$0")" && pwd)/update_index.sh" "$@"
