# ceph-prio-hub

MCP server for monitoring Ceph prio-list emails, consolidating issues, and tracking test coverage gaps.

## What It Does

- Monitors **ocs-prio-list**, **ceph-prio-list**, and **odf-prio-list** emails via Microsoft Graph API
- Consolidates email threads into unified issues (groups replies, matches case IDs across threads)
- Extracts structured data: case IDs, JIRA IDs, Ceph versions, components, error messages, stack traces
- Correlates with JIRA via `ceph-issue-kb` for issue status, labels (Ceph_L3, IBM_Customer_Issue), and comments
- Cross-references with `ceph-cmd-kb` and `ceph-doc-kb` for reproduction steps and coverage analysis
- Publishes an interactive dashboard + per-issue RCA reports to GitHub Pages
- Sanitizes all published data (redacts private IPs, customer names, internal domains)

## Setup

### 1. Azure AD App Registration (One-Time)

1. Go to [Azure Portal](https://portal.azure.com) > **App registrations** > **New registration**
2. Name: `Prio Email Monitor`
3. Supported account types: **Accounts in this organizational directory only**
4. No redirect URI needed
5. Go to **API permissions** > **Add a permission** > **Microsoft Graph** > **Delegated** > `Mail.Read`
6. Go to **Authentication** > Enable **Allow public client flows**
7. Copy the **Application (client) ID** and **Directory (tenant) ID**

### 2. Configure

```bash
mkdir -p ~/.ceph-prio-hub
cat > ~/.ceph-prio-hub/config.json << 'EOF'
{
  "client_id": "YOUR_CLIENT_ID_HERE",
  "tenant_id": "YOUR_TENANT_ID_HERE"
}
EOF
```

### 3. Install

```bash
cd ~/Projects/ceph-prio-hub
pip install -e .
```

### 4. Register in Cursor

The server is registered in `~/.cursor/mcp.json`:

```json
{
  "ceph-prio-hub": {
    "command": "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
    "args": ["-m", "ceph_prio_hub.server.mcp_server"],
    "cwd": "/Users/pawandhiran/Projects/ceph-prio-hub"
  }
}
```

### 5. First Authentication

On first use, the server will display a device code. Visit the URL shown and enter the code to authenticate with your Microsoft account. The token is cached so this only happens once.

## MCP Tools

| Tool | Description |
|------|-------------|
| `fetch_prio_emails` | Fetch recent emails from prio-lists (filterable by list, date range) |
| `get_email_details` | Get full email body + conversation thread for a message |
| `search_prio_emails` | Keyword search across prio-list emails |
| `extract_issue_info` | Parse email text to extract case IDs, versions, components, errors |
| `get_prio_stats` | Aggregate statistics on consolidated issues |
| `sync_issues` | Incremental sync — fetch new emails, consolidate into issues |
| `get_issue_timeline` | Full chronological timeline for a consolidated issue |
| `capabilities` | Server capabilities and prio-list configuration |
| `health` | Health check — Azure config, state, connectivity |

## Architecture

```
┌─────────────────────┐     ┌──────────────────┐     ┌────────────────┐
│  Microsoft Graph API │────▶│  ceph-prio-hub   │────▶│  GitHub Pages  │
│  (Outlook emails)    │     │  (MCP server)    │     │  (Dashboard)   │
└─────────────────────┘     └────────┬─────────┘     └────────────────┘
                                     │
                            ┌────────┴─────────┐
                            │  Ceph KB MCPs    │
                            │  • ceph-issue-kb │
                            │  • ceph-cmd-kb   │
                            │  • ceph-doc-kb   │
                            └──────────────────┘
```

## Development

```bash
pip install -e ".[dev]"
pytest
```
