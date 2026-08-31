# ceph-prio-hub

MCP server for **customer prio-list issue tracking**. Pulls IBMCEPH JIRA issues labelled `Ceph_L3` / `IBM_Customer_Issue`, consolidates prio-list email threads (ocs / ceph / odf), extracts case IDs and errors, records QA tracking, and can publish a dashboard.

This is **not** a general bug search. For “is this crash known?” use **ceph-issue-kb**. Use prio-hub when the work is a customer escalation, prio-list mail, or L3 tracking.

## For agents (read this first)

| Do | Do not |
|---|---|
| Sync and list L3 / IBM_Customer_Issue JIRA | Search the 18k+ issue corpus — **ceph-issue-kb** |
| Record analysis, repro steps, test coverage on a prio issue | Invent Ceph CLI — verify with **ceph-cmd-kb** |
| Fetch prio-list emails and timelines | Treat email as the primary source — JIRA is primary |
| Generate the HTML dashboard after tracking updates | Publish unsanitized customer data |

**Typical first calls**

1. `health()` — JIRA credentials and last sync.
2. `sync_jira_issues(since="YYYY-MM-DD")` or `fetch_jira_issues(since=...)` for a delta.
3. `get_jira_issue("IBMCEPH-xxxxx")` or `get_issue_timeline(case_id=...)`.
4. `update_tracking(...)` after analysis; then `generate_dashboard_tool()` if the user wants the site refreshed.

**Credentials (required for JIRA tools)**

Same env as ceph-issue-kb: `JIRA_USERNAME` and `JIRA_API_TOKEN`. Email tools additionally need Azure AD (`~/.ceph-prio-hub/config.json`).

## Ceph Engineering Intelligence Platform

| MCP | Cursor key | Use when | SSE port |
|-----|------------|----------|----------|
| **ceph-cmd-kb** | `ceph-cmd-kb` | Verify Ceph CLI, flags, configs | 8081 |
| **ceph-doc-kb** | `ceph-doc-kb` | How-to, architecture, IBM procedures | 8082 |
| **ceph-issue-kb** | `ceph-issue-kb` | Known bugs, workarounds, stacktraces | 8083 |
| **ceph-prio-hub** | `ceph-prio-hub` | Customer prio-list / L3 tracking | 8080 |
| **cephci-kb** | `cephci-kb` | CephCI tests, call graphs, YAML | 8084 |

Cross-MCP: after extracting an error from a prio email, call **ceph-issue-kb** `is_known_issue` / `search_issues`. For repro commands, verify with **ceph-cmd-kb**. For product procedure, **ceph-doc-kb**.

## Setup

### 1. Install

```bash
git clone https://github.com/pdhiran/ceph-prio-hub.git
cd ceph-prio-hub
pip install -e .
```

Put JIRA credentials in the environment (or in `~/Projects/ceph-issue-kb/.env`, which the MCP also loads):

```bash
export JIRA_USERNAME="you@ibm.com"
export JIRA_API_TOKEN="..."
```

### 2. Azure AD (email only — optional)

Email fetch needs a public-client Azure app with Microsoft Graph `Mail.Read` (delegated) and **Allow public client flows**. Then:

```bash
mkdir -p ~/.ceph-prio-hub
cat > ~/.ceph-prio-hub/config.json << 'EOF'
{
  "client_id": "YOUR_CLIENT_ID",
  "tenant_id": "YOUR_TENANT_ID"
}
EOF
```

On first email tool call the server prints a device code. Authenticate once; the token is cached under `~/.ceph-prio-hub/`.

JIRA-only workflows do **not** need Azure.

### 3. Incorporate into an agent

**Cursor** — `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "ceph-prio-hub": {
      "command": "python3",
      "args": ["-m", "ceph_prio_hub.server.mcp_server"],
      "cwd": "/path/to/ceph-prio-hub"
    }
  }
}
```

**Auto-update is on by default** (no extra flags needed): git pull of this repo on startup/interval, plus a `.reload_trigger` watcher so `./update_index.sh` hot-reloads without restarting Cursor. `--no-auto-update` disables **both** — including the trigger — so Cursor must restart the MCP subprocess after `./update_index.sh`. Interval: `--update-interval HOURS` (default `1`; `0` = startup pull only, trigger still watched). Details: [UPDATING.md](UPDATING.md).

**SSE** (Claude Desktop, Bob, Continue):

```bash
python3 -m ceph_prio_hub.server.mcp_server --transport sse --host 0.0.0.0 --port 8080
```

```json
{
  "mcpServers": {
    "ceph-prio-hub": {
      "url": "http://localhost:8080/sse",
      "transport": "sse"
    }
  }
}
```

State is stored in `~/.ceph-prio-hub/state/` (`issues.json`, `sync_metadata.json` — this is `STATE_DIR` / `config.state_dir`). QA tracking is `~/.ceph-prio-hub/tracking.json` (sibling of `state/`, used by `get_tracking` / `update_tracking`). `scripts/sync.py` writes `state/` only.

## Tool catalog

### JIRA (primary)

| Tool | Args | When to call |
|------|------|----------------|
| `fetch_jira_issues` | `labels="Ceph_L3,IBM_Customer_Issue"`, `since="YYYY-MM-DD"`, `status`, `component`, `limit=100` | List prio JIRA issues. Pass `since` for a date delta. |
| `sync_jira_issues` | `labels`, `since="YYYY-MM-DD"`, `limit=200` | Fetch and merge into local state. If `since` is omitted, uses last sync timestamp. |
| `get_jira_issue` | `issue_key` (e.g. `IBMCEPH-16204`) | Full issue + comments |

### Email (optional)

| Tool | Args | When to call |
|------|------|----------------|
| `fetch_prio_emails` | `prio_list="all\|ceph\|ocs\|odf"`, `days_back=7`, `limit=50`, `since="YYYY-MM-DD"` | Recent mail. `since` overrides `days_back`. |
| `get_email_details` | `message_id` | Full body + thread |
| `search_prio_emails` | `query`, `prio_list`, `days_back=30`, `limit=20` | Keyword search across lists |
| `extract_issue_info_tool` | `subject`, `body` | Parse case IDs, JIRA IDs, versions, components, errors, stack traces |

### Consolidation, tracking, dashboard

| Tool | Args | When to call |
|------|------|----------------|
| `sync_issues` | `days_back`, `limit=200` | Incremental email → consolidated issues. Omit `days_back` to use last sync. |
| `get_issue_timeline` | `issue_id` or `case_id` | Chronological emails + JIRA for one issue |
| `get_prio_stats` | (none) | Counts by component / list / severity |
| `get_tracking` | `issue_key` | QA analysis, repro, coverage, status |
| `update_tracking` | `issue_key` plus any of `qa_status`, `qa_assignee`, `internal_priority`, `analysis`, `repro_steps`, `test_coverage`, `hotfix_status`, `notes` | Additive update — empty strings are ignored |
| `list_tracking_status` | (none) | All assessed issues grouped by QA status |
| `generate_dashboard_tool` | `output_dir` (default `~/.ceph-prio-hub/site/`) | Write `index.html` + per-issue reports |
| `capabilities` | (none) | Sources and tool list |
| `health` | (none) | JIRA connectivity, Azure config, last sync, `state_dir`, `tracking_file` |

**`qa_status` values:** `not_assessed`, `needs_analysis`, `reproducing`, `test_written`, `verified`, `wont_fix`.

### Agent workflow: “sync prio issues since last week and analyse one”

1. `health()` — fail fast if JIRA is down.
2. `sync_jira_issues(since="2026-08-24")`
3. `fetch_jira_issues(since="2026-08-24", limit=50)` — pick a key.
4. `get_jira_issue("IBMCEPH-xxxxx")`
5. Optionally `search_issues` on **ceph-issue-kb** for duplicates.
6. `update_tracking(issue_key=..., analysis=..., repro_steps=..., qa_status="needs_analysis")`
7. `generate_dashboard_tool()` if the user wants the HTML site.

## Updating the knowledge base (delta dates)

Prio-hub state is live, not a frozen FAISS dump. `--since YYYY-MM-DD` is a JIRA filter: issues with `updated >=` that date (same ISO shape as issue-KB; not a git log).

```bash
# CLI (JIRA → ~/.ceph-prio-hub/state/)
python3 scripts/sync.py --since 2026-08-01 --verbose
python3 scripts/sync.py --since 2026-08-01 --emails   # also Graph mail
python3 scripts/sync.py                                # since last sync timestamp

# Canonical wrapper (last-run tracker + .reload_trigger)
./update_index.sh                 # 1-day overlap from last success, or last 1 day
./update_index.sh 7
./update_index.sh 2026-08-01
./update_index.sh --reset
```

Full maintainer help (hot-reload, `--no-auto-update` kills the trigger): [UPDATING.md](UPDATING.md).

Equivalent MCP calls:

```text
sync_jira_issues(since="2026-08-01")
fetch_prio_emails(since="2026-08-01", limit=50)
sync_issues(days_back=7)          # email consolidation; uses last_sync if days_back omitted
```

## Architecture

```
ibm-ceph.atlassian.net (IBMCEPH, labels Ceph_L3 / IBM_Customer_Issue)
        │
        ▼
 JiraClient  ──►  IssueStateDB (~/.ceph-prio-hub/state/)  ──┐
                                                             ├──► generate_dashboard → HTML site
 TrackingDB (~/.ceph-prio-hub/tracking.json)  ─────────────┘

 Microsoft Graph (optional) ──► prio-list mailboxes
```

Published dashboard HTML is sanitized (IPs, customer names, internal domains redacted). Do not bypass the sanitizer when publishing.

## Development

```bash
pip install -e ".[dev]"
pytest
```
