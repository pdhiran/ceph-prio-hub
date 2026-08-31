# Updating prio-hub state

This is the maintainer help for **ceph-prio-hub**. Agents and humans: use this page when refreshing JIRA/email state. Do not invent a different workflow.

Prio-hub is **live state**, not a frozen FAISS dump. Canonical disk layout under `~/.ceph-prio-hub/`:

| File | Path | Writer |
|---|---|---|
| `issues.json`, `sync_metadata.json` | `~/.ceph-prio-hub/state/` (`STATE_DIR` / `config.state_dir`) | `scripts/sync.py`, MCP `sync_jira_issues` |
| `tracking.json` | `~/.ceph-prio-hub/tracking.json` (sibling of `state/`, **not** inside it) | MCP `update_tracking` — `sync.py` does **not** write this |

Git holds the MCP **code**, not the issue database.

Cursor does **not** need a restart after `./update_index.sh`. The MCP re-reads state from disk when it sees `.reload_trigger`. If git pull brings `.py` changes, Cursor respawns the MCP subprocess.

## Canonical command

Needs `JIRA_USERNAME` / `JIRA_API_TOKEN` (same as ceph-issue-kb; `.env` in this repo or `~/Projects/ceph-issue-kb/.env`).

```bash
cd /path/to/ceph-prio-hub
./update_index.sh                 # since yesterday of last success (1-day overlap), or last 1 day if first run
./update_index.sh 7               # last 7 days
./update_index.sh 2026-08-01      # explicit ISO date
./update_index.sh --reset         # clear .last_index_update
```

`./update_kb.sh` is a thin wrapper that execs `./update_index.sh`. Prefer `./update_index.sh`.

## What `./update_index.sh` does

1. Resolves `--since`.
2. Runs `python3 scripts/sync.py --since DATE --verbose` (JIRA → `~/.ceph-prio-hub/state/`).
3. Touches `.reload_trigger` in the **git repo root**.
4. Writes `.last_index_update` to **yesterday of the run date** (1-day overlap), not the ISO `--since` you passed.

Add `--emails` only via the Python CLI if you also want Graph mail (Azure must be configured):

```bash
python3 scripts/sync.py --since 2026-08-01 --emails --verbose
touch .reload_trigger
```

## How the running MCP picks up new state (no Cursor restart)

| Event | What the MCP does | Cursor |
|---|---|---|
| `./update_index.sh` (separate process wrote `~/.ceph-prio-hub/state/`) | Trigger watcher (~5s) calls `IssueStateDB.reload()` (reads `self._state_dir` = `config.state_dir`) + `TrackingDB.reload()` (reads `config.tracking_file`) | Stays open |
| `git pull` of this repo, `*.py` changed | MCP `os._exit(0)`; Cursor respawns the subprocess | Stays open |
| `git pull` of non-Python files | Hot-reload DBs from disk | Stays open |
| Periodic timer (default 1h) | Git pull; if `JIRA_USERNAME` + `JIRA_API_TOKEN` are set, JIRA delta into the in-process DB | Stays open |
| No git remote | Pull skipped; trigger watcher still runs | Stays open |

Disable: `--no-auto-update` (skips git pull **and** the trigger watcher). Interval: `--update-interval HOURS` (default 1).

### Cursor MCP config

```json
{
  "command": "python",
  "args": ["-m", "ceph_prio_hub.server.mcp_server", "--auto-update", "--update-interval", "1"]
}
```

## Equivalent MCP tools (no shell)

```text
sync_jira_issues(since="2026-08-01")
fetch_prio_emails(since="2026-08-01", limit=50)
sync_issues(days_back=7)
```

Those mutate the in-process DB and `db.save()` to `~/.ceph-prio-hub/state/`. `./update_index.sh` writes that same directory from another process — that is why `.reload_trigger` exists.

## Files that must stay untracked

`.reload_trigger`, `.last_index_update`, `.env`, `state/`, `config.json`. Never commit `~/.ceph-prio-hub/`.

## Troubleshooting

| Symptom | Check |
|---|---|
| MCP tools still show pre-sync issues | Wait 5s after trigger; confirm MCP cwd/repo is this checkout |
| Periodic JIRA never runs | Env vars not visible to the MCP process. `mcp_server.py` loads repo `.env` and `~/Projects/ceph-issue-kb/.env` via dotenv; Cursor MCP `env` does **not** auto-load your shell. If those files are missing, `_maybe_sync_jira` is skipped. `.reload_trigger` still works — it only re-reads disk. |
| Azure mail not fetched | `scripts/sync.py --emails` plus `~/.ceph-prio-hub/config.json` |
