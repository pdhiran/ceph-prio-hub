"""Dashboard and per-issue report generator.

Reads the consolidated issue state DB and produces:
1. index.html — main dashboard with summary metrics, charts, and issue table
2. issues/<issue_id>.html — per-issue detail/RCA report

All HTML is self-contained (inline CSS, CDN-loaded Chart.js) and follows
the IBM Carbon design system.
"""

from __future__ import annotations

import json
import html as html_lib
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from ceph_prio_hub.tracker.state import IssueStateDB, ConsolidatedIssue
from ceph_prio_hub.sanitizer.redactor import sanitize_text


def generate_dashboard(db: IssueStateDB, output_dir: Path) -> Path:
    """Generate the full dashboard site into output_dir.

    Returns path to index.html.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    issues_dir = output_dir / "issues"
    issues_dir.mkdir(exist_ok=True)

    issues = db.get_all_issues()
    issue_data = [_build_issue_row(i) for i in issues]
    issue_data.sort(key=lambda x: x["updated"], reverse=True)

    stats = _compute_stats(issue_data)

    index_html = _render_index(issue_data, stats)
    index_path = output_dir / "index.html"
    index_path.write_text(index_html, encoding="utf-8")

    for row in issue_data:
        issue = next((i for i in issues if i.issue_id == row["issue_id"]), None)
        if issue:
            report = _render_issue_report(row, issue)
            (issues_dir / f"{row['issue_id']}.html").write_text(report, encoding="utf-8")

    # Write issues.json for GitHub API-based tracking edits
    tracking_path = output_dir / "issues.json"
    tracking_data = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "issues": [{
            "issue_id": r["issue_id"],
            "jira_key": r["jira_key"],
            "summary": r["summary"],
            "status": r["status"],
            "status_category": r["status_category"],
        } for r in issue_data],
    }
    tracking_path.write_text(json.dumps(tracking_data, indent=2), encoding="utf-8")

    return index_path


def _build_issue_row(issue: ConsolidatedIssue) -> dict[str, Any]:
    d = issue._data
    timeline = d.get("timeline", [])
    jstatus_entries = [e for e in timeline if e.get("type") == "jira_status"]
    last_status = jstatus_entries[-1] if jstatus_entries else {}

    jira_ids = d.get("jira_ids", [])
    jira_key = jira_ids[0] if jira_ids else ""

    return {
        "issue_id": issue.issue_id,
        "jira_key": jira_key,
        "jira_url": f"https://ibm-ceph.atlassian.net/browse/{jira_key}" if jira_key else "",
        "summary": sanitize_text(d.get("subject", "")),
        "status": last_status.get("status", "Unknown"),
        "status_category": last_status.get("status_category", "Unknown"),
        "assignee": sanitize_text(last_status.get("assignee", "")),
        "priority": d.get("severity", "normal"),
        "components": d.get("components", []),
        "labels": d.get("jira_labels", []),
        "versions": d.get("ceph_versions", []),
        "created": d.get("first_seen", ""),
        "updated": d.get("last_updated", ""),
        "timeline_count": len(timeline),
        "comment_count": len([e for e in timeline if e.get("type") == "jira_comment"]),
    }


def _compute_stats(rows: list[dict]) -> dict[str, Any]:
    total = len(rows)
    status_counts = Counter(r["status_category"] for r in rows)
    priority_counts = Counter(r["priority"] for r in rows)
    component_counts: Counter = Counter()
    label_counts: Counter = Counter()

    for r in rows:
        for c in r["components"]:
            component_counts[c] += 1
        for l in r["labels"]:
            label_counts[l] += 1

    open_count = status_counts.get("To Do", 0) + status_counts.get("In Progress", 0)
    closed_count = status_counts.get("Done", 0)

    return {
        "total": total,
        "open": open_count,
        "in_progress": status_counts.get("In Progress", 0),
        "closed": closed_count,
        "blocker": priority_counts.get("blocker", 0),
        "major": priority_counts.get("major", 0),
        "status_counts": dict(status_counts.most_common()),
        "priority_counts": dict(priority_counts.most_common()),
        "component_counts": dict(component_counts.most_common(20)),
        "label_counts": dict(label_counts.most_common(15)),
    }


def _esc(text: str) -> str:
    return html_lib.escape(str(text))


def _status_badge(status: str, category: str) -> str:
    color_map = {
        "Done": "#198038",
        "In Progress": "#0f62fe",
        "To Do": "#da1e28",
    }
    color = color_map.get(category, "#525252")
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:12px;'
        f'font-size:0.78rem;font-weight:500;color:#fff;background:{color};">'
        f'{_esc(status)}</span>'
    )


def _priority_badge(priority: str) -> str:
    color_map = {
        "blocker": "#da1e28",
        "major": "#ff832b",
        "normal": "#0f62fe",
        "minor": "#198038",
    }
    color = color_map.get(priority.lower(), "#525252")
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:4px;'
        f'font-size:0.75rem;font-weight:500;color:{color};background:{color}18;'
        f'border:1px solid {color}40;">{_esc(priority)}</span>'
    )


def _label_badges(labels: list[str]) -> str:
    parts = []
    highlight = {"Ceph_L3", "IBM_Customer_Issue", "Hotfix_requested", "Hotfix_Delivered", "Regression"}
    for lbl in labels:
        if lbl in highlight:
            color = "#8a3ffc" if lbl.startswith("Ceph") or lbl.startswith("IBM") else "#da1e28" if "Regression" in lbl else "#009d9a"
            parts.append(
                f'<span style="display:inline-block;padding:1px 6px;border-radius:3px;'
                f'font-size:0.7rem;color:{color};background:{color}12;border:1px solid {color}30;'
                f'margin:1px 2px;">{_esc(lbl)}</span>'
            )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------

def _render_index(rows: list[dict], stats: dict) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    rows_json = json.dumps(rows, default=str)
    stats_json = json.dumps(stats, default=str)

    issue_rows_html = []
    for r in rows:
        comp_str = ", ".join(r["components"][:3])
        created_short = r["created"][:10] if r["created"] else ""
        updated_short = r["updated"][:10] if r["updated"] else ""
        issue_rows_html.append(f"""<tr class="issue-row"
            data-status="{_esc(r['status_category'])}"
            data-priority="{_esc(r['priority'])}"
            data-component="{_esc(','.join(r['components']))}"
            data-labels="{_esc(','.join(r['labels']))}">
          <td><a href="issues/{r['issue_id']}.html" style="color:var(--ibm-blue);text-decoration:none;font-weight:500;">{_esc(r['jira_key'])}</a></td>
          <td style="max-width:340px;">{_esc(r['summary'][:100])}</td>
          <td>{_status_badge(r['status'], r['status_category'])}</td>
          <td>{_priority_badge(r['priority'])}</td>
          <td style="font-size:0.82rem;">{_esc(comp_str)}</td>
          <td style="font-size:0.82rem;">{_esc(r['assignee'])}</td>
          <td style="font-size:0.82rem;">{_label_badges(r['labels'])}</td>
          <td style="font-size:0.8rem;color:var(--ibm-gray-50);">{created_short}</td>
          <td style="font-size:0.8rem;color:var(--ibm-gray-50);">{updated_short}</td>
        </tr>""")

    table_rows = "\n".join(issue_rows_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ceph Prio-Hub Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root {{
  --ibm-blue: #0f62fe;
  --ibm-dark: #001d6c;
  --ibm-darker: #000a3d;
  --ibm-teal: #009d9a;
  --ibm-light-blue: #e8f0fe;
  --ibm-hover: #0353e9;
  --ibm-gray-10: #f4f4f4;
  --ibm-gray-20: #e0e0e0;
  --ibm-gray-30: #c6c6c6;
  --ibm-gray-50: #8d8d8d;
  --ibm-gray-70: #525252;
  --ibm-gray-90: #262626;
  --ibm-gray-100: #161616;
  --ibm-red: #da1e28;
  --ibm-green: #198038;
  --ibm-purple: #8a3ffc;
  --ibm-orange: #ff832b;
  --sidebar-width: 240px;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'IBM Plex Sans', -apple-system, sans-serif; font-size: 14px; line-height: 1.5; color: var(--ibm-gray-90); background: #fff; }}

.header {{
  position: fixed; top: 0; left: 0; right: 0; height: 48px; z-index: 100;
  background: var(--ibm-dark); color: #fff;
  display: flex; align-items: center; padding: 0 1.5rem;
  box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}}
.header h1 {{ font-size: 0.95rem; font-weight: 600; letter-spacing: 0.02em; }}
.header .gen-date {{ margin-left: auto; font-size: 0.75rem; opacity: 0.7; }}

.sidebar {{
  position: fixed; top: 48px; left: 0; bottom: 0; width: var(--sidebar-width);
  background: var(--ibm-gray-10); border-right: 1px solid var(--ibm-gray-20);
  overflow-y: auto; padding: 1.5rem 0; z-index: 90;
}}
.sidebar nav a {{
  display: block; padding: 0.5rem 1.5rem; color: var(--ibm-gray-70);
  text-decoration: none; font-size: 0.82rem; border-left: 3px solid transparent;
  transition: all 0.15s ease;
}}
.sidebar nav a:hover {{ color: var(--ibm-blue); background: rgba(15,98,254,0.04); }}
.sidebar nav a.active {{
  color: var(--ibm-blue); font-weight: 600;
  border-left-color: var(--ibm-blue); background: rgba(15,98,254,0.06);
}}

.main {{
  margin-left: var(--sidebar-width); margin-top: 48px;
  padding: 2rem 2.5rem; max-width: 1400px;
}}

.metrics-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1rem; margin: 1.5rem 0;
}}
.metric-card {{
  background: var(--ibm-gray-10); border-radius: 6px; padding: 1.2rem;
  text-align: center; border-top: 3px solid var(--ibm-blue);
}}
.metric-card.red {{ border-top-color: var(--ibm-red); }}
.metric-card.green {{ border-top-color: var(--ibm-green); }}
.metric-card.orange {{ border-top-color: var(--ibm-orange); }}
.metric-card.purple {{ border-top-color: var(--ibm-purple); }}
.metric-card.teal {{ border-top-color: var(--ibm-teal); }}
.metric-value {{ font-size: 2rem; font-weight: 700; color: var(--ibm-blue); }}
.metric-card.red .metric-value {{ color: var(--ibm-red); }}
.metric-card.green .metric-value {{ color: var(--ibm-green); }}
.metric-card.orange .metric-value {{ color: var(--ibm-orange); }}
.metric-card.purple .metric-value {{ color: var(--ibm-purple); }}
.metric-card.teal .metric-value {{ color: var(--ibm-teal); }}
.metric-label {{ font-size: 0.78rem; color: var(--ibm-gray-70); margin-top: 0.2rem; text-transform: uppercase; letter-spacing: 0.05em; }}

.charts-row {{
  display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin: 1.5rem 0;
}}
.chart-container {{
  background: var(--ibm-gray-10); border-radius: 6px; padding: 1.2rem;
  height: 320px; position: relative;
}}
.chart-title {{ font-size: 0.82rem; font-weight: 600; color: var(--ibm-gray-70); margin-bottom: 0.5rem; text-transform: uppercase; letter-spacing: 0.04em; }}

.filters {{
  display: flex; gap: 0.75rem; margin: 1.5rem 0; flex-wrap: wrap; align-items: center;
}}
.filters select, .filters input {{
  font-family: inherit; font-size: 0.82rem; padding: 0.4rem 0.75rem;
  border: 1px solid var(--ibm-gray-30); border-radius: 4px; background: #fff;
  color: var(--ibm-gray-90);
}}
.filters select:focus, .filters input:focus {{ outline: 2px solid var(--ibm-blue); border-color: transparent; }}
.filters label {{ font-size: 0.78rem; color: var(--ibm-gray-70); font-weight: 500; }}
.filter-count {{ font-size: 0.82rem; color: var(--ibm-gray-50); margin-left: auto; }}

table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
th {{
  background: var(--ibm-gray-10); font-weight: 600; text-align: left;
  padding: 0.6rem 0.75rem; border-bottom: 2px solid var(--ibm-gray-20);
  position: sticky; top: 0; cursor: pointer; white-space: nowrap;
  user-select: none;
}}
th:hover {{ color: var(--ibm-blue); }}
td {{ padding: 0.55rem 0.75rem; border-bottom: 1px solid var(--ibm-gray-20); vertical-align: top; }}
tr:hover {{ background: rgba(15,98,254,0.02); }}

section {{ scroll-margin-top: 70px; }}
h2 {{ font-size: 1.15rem; font-weight: 600; color: var(--ibm-gray-90); margin: 2rem 0 0.75rem; padding-bottom: 0.4rem; border-bottom: 1px solid var(--ibm-gray-20); }}

@media (max-width: 900px) {{
  .sidebar {{ display: none; }}
  .main {{ margin-left: 0; }}
  .charts-row {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>

<div class="header">
  <h1>Ceph Prio-Hub &mdash; Customer Issue Dashboard</h1>
  <span class="gen-date">Generated: {now}</span>
</div>

<div class="sidebar">
  <nav>
    <a href="#summary" class="active">Summary</a>
    <a href="#charts">Analytics</a>
    <a href="#issues">Issues ({stats['total']})</a>
  </nav>
  <div style="padding:1.5rem;font-size:0.75rem;color:var(--ibm-gray-50);border-top:1px solid var(--ibm-gray-20);margin-top:1rem;">
    Source: IBMCEPH JIRA<br>
    Labels: Ceph_L3, IBM_Customer_Issue
  </div>
</div>

<div class="main">

<section id="summary">
  <div style="background:linear-gradient(135deg, var(--ibm-dark) 0%, #002d9c 100%); color:#fff; border-radius:8px; padding:2rem 2.5rem; margin-bottom:1.5rem;">
    <div style="font-size:1rem;font-weight:300;opacity:0.85;">Ceph Prio-Hub &mdash; Customer Escalation Tracker</div>
    <div style="font-size:1.5rem;font-weight:600;margin-top:0.5rem;">
      <span style="color:#78a9ff;">{stats['total']}</span> tracked issues &mdash;
      <span style="color:#78a9ff;">{stats['open']}</span> open,
      <span style="color:#78a9ff;">{stats['closed']}</span> resolved
    </div>
    <div style="margin-top:0.75rem;font-size:0.9rem;opacity:0.9;">
      Tracking customer escalations from IBMCEPH JIRA with Ceph_L3 and IBM_Customer_Issue labels.
      Includes {stats['blocker']} blockers and {stats['major']} major-priority issues.
    </div>
  </div>

  <div class="metrics-grid">
    <div class="metric-card"><div class="metric-value">{stats['total']}</div><div class="metric-label">Total Issues</div></div>
    <div class="metric-card red"><div class="metric-value">{stats['open']}</div><div class="metric-label">Open</div></div>
    <div class="metric-card"><div class="metric-value">{stats['in_progress']}</div><div class="metric-label">In Progress</div></div>
    <div class="metric-card green"><div class="metric-value">{stats['closed']}</div><div class="metric-label">Resolved</div></div>
    <div class="metric-card orange"><div class="metric-value">{stats['blocker']}</div><div class="metric-label">Blockers</div></div>
    <div class="metric-card purple"><div class="metric-value">{stats['major']}</div><div class="metric-label">Major</div></div>
  </div>
</section>

<section id="charts">
  <h2>Analytics</h2>
  <div class="charts-row">
    <div class="chart-container">
      <div class="chart-title">Issues by Component</div>
      <canvas id="chart-components"></canvas>
    </div>
    <div class="chart-container">
      <div class="chart-title">Status Distribution</div>
      <canvas id="chart-status"></canvas>
    </div>
  </div>
  <div class="charts-row">
    <div class="chart-container">
      <div class="chart-title">Priority Distribution</div>
      <canvas id="chart-priority"></canvas>
    </div>
    <div class="chart-container">
      <div class="chart-title">Issues by Label</div>
      <canvas id="chart-labels"></canvas>
    </div>
  </div>
</section>

<section id="issues">
  <h2>All Issues</h2>

  <div class="filters">
    <label>Status:
      <select id="filter-status">
        <option value="">All</option>
        <option value="To Do">Open</option>
        <option value="In Progress">In Progress</option>
        <option value="Done">Resolved</option>
      </select>
    </label>
    <label>Priority:
      <select id="filter-priority">
        <option value="">All</option>
        <option value="blocker">Blocker</option>
        <option value="major">Major</option>
        <option value="normal">Normal</option>
        <option value="minor">Minor</option>
      </select>
    </label>
    <label>Component:
      <select id="filter-component">
        <option value="">All</option>
      </select>
    </label>
    <label>Label:
      <select id="filter-label">
        <option value="">All</option>
      </select>
    </label>
    <label>
      <input type="text" id="filter-search" placeholder="Search summary..." style="min-width:200px;">
    </label>
    <span class="filter-count" id="visible-count">{stats['total']} issues</span>
  </div>

  <table id="issue-table">
    <thead>
      <tr>
        <th data-sort="key">Key</th>
        <th data-sort="summary">Summary</th>
        <th data-sort="status">Status</th>
        <th data-sort="priority">Priority</th>
        <th data-sort="component">Components</th>
        <th data-sort="assignee">Assignee</th>
        <th>Labels</th>
        <th data-sort="created">Created</th>
        <th data-sort="updated">Updated</th>
      </tr>
    </thead>
    <tbody>
      {table_rows}
    </tbody>
  </table>
</section>

</div>

<script>
const STATS = {stats_json};
const ISSUES = {rows_json};

// --- Charts ---
const IBM_COLORS = ['#0f62fe','#009d9a','#8a3ffc','#ff832b','#da1e28','#198038','#002d9c','#b28600','#6929c4','#1192e8','#fa4d56','#005d5d'];

function makeChart(id, type, labels, data, opts) {{
  const ctx = document.getElementById(id);
  if (!ctx) return;
  new Chart(ctx, {{
    type: type,
    data: {{
      labels: labels,
      datasets: [{{
        data: data,
        backgroundColor: opts.colors || IBM_COLORS.slice(0, data.length),
        borderWidth: 0,
        borderRadius: type === 'bar' ? 4 : 0,
      }}]
    }},
    options: {{
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: type === 'doughnut', position: 'right', labels: {{ font: {{ size: 11 }} }} }} }},
      scales: type === 'bar' ? {{
        y: {{ beginAtZero: true, grid: {{ color: '#e0e0e0' }} }},
        x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 10 }}, maxRotation: 45 }} }}
      }} : undefined,
    }}
  }});
}}

const compLabels = Object.keys(STATS.component_counts).slice(0, 12);
const compData = compLabels.map(k => STATS.component_counts[k]);
makeChart('chart-components', 'bar', compLabels, compData, {{}});

const statusLabels = Object.keys(STATS.status_counts);
const statusData = statusLabels.map(k => STATS.status_counts[k]);
makeChart('chart-status', 'doughnut', statusLabels, statusData, {{ colors: ['#da1e28','#0f62fe','#198038'] }});

const prioLabels = Object.keys(STATS.priority_counts);
const prioData = prioLabels.map(k => STATS.priority_counts[k]);
makeChart('chart-priority', 'doughnut', prioLabels, prioData, {{ colors: ['#0f62fe','#ff832b','#da1e28','#198038'] }});

const lblLabels = Object.keys(STATS.label_counts).slice(0, 10);
const lblData = lblLabels.map(k => STATS.label_counts[k]);
makeChart('chart-labels', 'bar', lblLabels, lblData, {{}});

// --- Populate filter dropdowns ---
const compSet = new Set();
const lblSet = new Set();
ISSUES.forEach(i => {{
  (i.components || []).forEach(c => compSet.add(c));
  (i.labels || []).forEach(l => lblSet.add(l));
}});
const compSelect = document.getElementById('filter-component');
[...compSet].sort().forEach(c => {{ const o = document.createElement('option'); o.value = c; o.textContent = c; compSelect.appendChild(o); }});
const lblSelect = document.getElementById('filter-label');
[...lblSet].sort().forEach(l => {{ const o = document.createElement('option'); o.value = l; o.textContent = l; lblSelect.appendChild(o); }});

// --- Filtering ---
function applyFilters() {{
  const status = document.getElementById('filter-status').value;
  const priority = document.getElementById('filter-priority').value;
  const component = document.getElementById('filter-component').value;
  const label = document.getElementById('filter-label').value;
  const search = document.getElementById('filter-search').value.toLowerCase();
  let visible = 0;
  document.querySelectorAll('.issue-row').forEach(row => {{
    let show = true;
    if (status && row.dataset.status !== status) show = false;
    if (priority && row.dataset.priority !== priority) show = false;
    if (component && !row.dataset.component.includes(component)) show = false;
    if (label && !row.dataset.labels.includes(label)) show = false;
    if (search && !row.children[1].textContent.toLowerCase().includes(search)) show = false;
    row.style.display = show ? '' : 'none';
    if (show) visible++;
  }});
  document.getElementById('visible-count').textContent = visible + ' issues';
}}
['filter-status','filter-priority','filter-component','filter-label'].forEach(id => {{
  document.getElementById(id).addEventListener('change', applyFilters);
}});
document.getElementById('filter-search').addEventListener('input', applyFilters);

// --- Column sorting ---
let sortCol = '', sortAsc = true;
document.querySelectorAll('th[data-sort]').forEach(th => {{
  th.addEventListener('click', () => {{
    const col = th.dataset.sort;
    if (sortCol === col) sortAsc = !sortAsc;
    else {{ sortCol = col; sortAsc = true; }}
    const tbody = document.querySelector('#issue-table tbody');
    const rows = [...tbody.querySelectorAll('tr')];
    const colIdx = [...th.parentNode.children].indexOf(th);
    rows.sort((a, b) => {{
      const aT = a.children[colIdx].textContent.trim();
      const bT = b.children[colIdx].textContent.trim();
      return sortAsc ? aT.localeCompare(bT) : bT.localeCompare(aT);
    }});
    rows.forEach(r => tbody.appendChild(r));
  }});
}});

// --- Scroll spy ---
const sections = document.querySelectorAll('section[id]');
const navLinks = document.querySelectorAll('.sidebar nav a');
window.addEventListener('scroll', () => {{
  let current = '';
  sections.forEach(s => {{ if (window.scrollY >= s.offsetTop - 100) current = s.id; }});
  navLinks.forEach(l => l.classList.toggle('active', l.getAttribute('href') === '#' + current));
}});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Per-issue report
# ---------------------------------------------------------------------------

def _render_issue_report(row: dict, issue: ConsolidatedIssue) -> str:
    d = issue._data
    timeline = d.get("timeline", [])
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    timeline_html_parts = []
    for entry in timeline:
        etype = entry.get("type", "")
        edate = entry.get("date", "")[:19].replace("T", " ")
        if etype == "jira_status":
            timeline_html_parts.append(
                f'<div class="tl-entry"><span class="tl-date">{_esc(edate)}</span> '
                f'<span class="tl-badge status">Status</span> '
                f'{_esc(entry.get("status", ""))} '
                f'<span style="color:var(--ibm-gray-50);">({_esc(entry.get("assignee", ""))})</span></div>'
            )
        elif etype == "jira_comment":
            body = sanitize_text(entry.get("summary", ""))
            timeline_html_parts.append(
                f'<div class="tl-entry"><span class="tl-date">{_esc(edate)}</span> '
                f'<span class="tl-badge comment">Comment</span> '
                f'<strong>{_esc(entry.get("author", ""))}</strong>: '
                f'{_esc(body[:200])}</div>'
            )
        elif etype == "email":
            timeline_html_parts.append(
                f'<div class="tl-entry"><span class="tl-date">{_esc(edate)}</span> '
                f'<span class="tl-badge email">Email</span> '
                f'from {_esc(entry.get("from", ""))}: {_esc(entry.get("subject", "")[:100])}</div>'
            )

    timeline_html = "\n".join(timeline_html_parts) if timeline_html_parts else "<p style='color:var(--ibm-gray-50);'>No timeline entries.</p>"

    jira_link = f'<a href="{_esc(row["jira_url"])}" target="_blank" style="color:var(--ibm-blue);">{_esc(row["jira_key"])}</a>' if row["jira_url"] else "N/A"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(row['jira_key'])} - {_esc(row['summary'][:60])}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {{
  --ibm-blue: #0f62fe; --ibm-dark: #001d6c; --ibm-teal: #009d9a;
  --ibm-gray-10: #f4f4f4; --ibm-gray-20: #e0e0e0; --ibm-gray-50: #8d8d8d;
  --ibm-gray-70: #525252; --ibm-gray-90: #262626; --ibm-gray-100: #161616;
  --ibm-red: #da1e28; --ibm-green: #198038; --ibm-purple: #8a3ffc; --ibm-orange: #ff832b;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: 'IBM Plex Sans', sans-serif; font-size: 14px; line-height: 1.6; color: var(--ibm-gray-90); background: #fff; padding: 2rem; max-width: 960px; margin: 0 auto; }}
.back {{ font-size: 0.85rem; color: var(--ibm-blue); text-decoration: none; }}
.back:hover {{ text-decoration: underline; }}
h1 {{ font-size: 1.3rem; font-weight: 600; margin: 1rem 0 0.5rem; }}
.meta {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin: 1.5rem 0; }}
.meta-card {{ background: var(--ibm-gray-10); border-radius: 6px; padding: 1rem 1.2rem; }}
.meta-card dt {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ibm-gray-50); font-weight: 600; }}
.meta-card dd {{ font-size: 0.95rem; margin-top: 0.2rem; }}
h2 {{ font-size: 1rem; font-weight: 600; margin: 2rem 0 0.75rem; padding-bottom: 0.3rem; border-bottom: 1px solid var(--ibm-gray-20); }}
.tl-entry {{ padding: 0.6rem 0; border-bottom: 1px solid var(--ibm-gray-20); font-size: 0.85rem; }}
.tl-date {{ font-family: 'IBM Plex Mono', monospace; font-size: 0.78rem; color: var(--ibm-gray-50); margin-right: 0.5rem; }}
.tl-badge {{
  display: inline-block; padding: 1px 8px; border-radius: 3px; font-size: 0.72rem;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; margin-right: 0.3rem;
}}
.tl-badge.status {{ background: #e8f0fe; color: var(--ibm-blue); }}
.tl-badge.comment {{ background: #e6f4ea; color: var(--ibm-green); }}
.tl-badge.email {{ background: #f3e8fd; color: var(--ibm-purple); }}
.label-list span {{
  display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 0.75rem;
  background: var(--ibm-gray-10); border: 1px solid var(--ibm-gray-20); margin: 2px;
}}
</style>
</head>
<body>
<a href="../index.html" class="back">&larr; Back to Dashboard</a>
<h1>{_esc(row['jira_key'])}: {_esc(row['summary'])}</h1>
<p style="font-size:0.82rem;color:var(--ibm-gray-50);">Generated: {now}</p>

<div class="meta">
  <div class="meta-card"><dl>
    <dt>JIRA</dt><dd>{jira_link}</dd>
    <dt>Status</dt><dd>{_status_badge(row['status'], row['status_category'])}</dd>
    <dt>Priority</dt><dd>{_priority_badge(row['priority'])}</dd>
  </dl></div>
  <div class="meta-card"><dl>
    <dt>Assignee</dt><dd>{_esc(row['assignee']) or 'Unassigned'}</dd>
    <dt>Components</dt><dd>{_esc(', '.join(row['components']))}</dd>
    <dt>Created</dt><dd>{_esc(row['created'][:10])}</dd>
    <dt>Updated</dt><dd>{_esc(row['updated'][:10])}</dd>
  </dl></div>
</div>

<div style="margin:1rem 0;">
  <strong style="font-size:0.82rem;color:var(--ibm-gray-70);">Labels:</strong>
  <div class="label-list">{''.join(f'<span>{_esc(l)}</span>' for l in row['labels'])}</div>
</div>

<h2>Timeline ({len(timeline)} events)</h2>
<div class="timeline">
{timeline_html}
</div>

</body>
</html>"""
