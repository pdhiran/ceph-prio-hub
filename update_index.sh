#!/bin/bash
# Incremental prio-hub sync. Same delta-date contract as ceph-issue-kb:
#   ./update_index.sh              # since last successful run (or last 1 day)
#   ./update_index.sh 7            # last 7 days
#   ./update_index.sh 2026-08-01   # since a specific ISO date
#   ./update_index.sh --reset      # clear the last-run tracker
#
# Requires JIRA_USERNAME / JIRA_API_TOKEN (same as ceph-issue-kb).
# Writes ~/.ceph-prio-hub/ then touches .reload_trigger so a running MCP
# re-reads state from disk without restarting Cursor.

set -euo pipefail
cd "$(dirname "$0")"

LAST_RUN_FILE=".last_index_update"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

if [[ "${1:-}" == "--reset" ]]; then
    rm -f "$LAST_RUN_FILE"
    echo "Last-run tracker reset. Next run will fetch last 1 day."
    exit 0
fi

if [[ -n "${1:-}" ]]; then
    ARG="$1"
    if [[ "$ARG" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        SINCE="$ARG"
    else
        SINCE=$(date -v-"${ARG}"d +%Y-%m-%d 2>/dev/null || date -d "${ARG} days ago" +%Y-%m-%d)
    fi
elif [[ -f "$LAST_RUN_FILE" ]]; then
    SINCE=$(cat "$LAST_RUN_FILE")
    echo "(Last successful run: $SINCE)"
else
    SINCE=$(date -v-1d +%Y-%m-%d 2>/dev/null || date -d "1 day ago" +%Y-%m-%d)
    echo "(First run — fetching last 1 day)"
fi

echo "=== Ceph Prio-Hub Update ==="
echo "Delta since: $SINCE"
echo ""

python3 scripts/sync.py --since "$SINCE" --verbose

touch .reload_trigger

date -v-1d +%Y-%m-%d > "$LAST_RUN_FILE" 2>/dev/null || date -d "1 day ago" +%Y-%m-%d > "$LAST_RUN_FILE"

echo ""
echo "=== Prio-hub synced since $SINCE ==="
echo "Touched .reload_trigger — running MCP hot-reloads within ~5s (no Cursor restart)."
