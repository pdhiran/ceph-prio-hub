"""Dashboard and per-issue report generator.

Reads the consolidated issue state DB and tracking DB to produce:
1. index.html — dashboard with metrics, charts, filterable issue table + QA columns
2. issues/<issue_id>.html — per-issue report with analysis, repro steps, test coverage, timeline

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
from ceph_prio_hub.tracker.tracking import TrackingDB, QA_STATUS_LABELS, QA_STATUSES
from ceph_prio_hub.sanitizer.redactor import sanitize_text
from ceph_prio_hub.team import TEAM_NAMES


def generate_dashboard(
    db: IssueStateDB,
    output_dir: Path,
    tracking: TrackingDB | None = None,
) -> Path:
    """Generate the full dashboard site into output_dir."""
    output_dir.mkdir(parents=True, exist_ok=True)
    issues_dir = output_dir / "issues"
    issues_dir.mkdir(exist_ok=True)

    if tracking is None:
        tracking = TrackingDB()

    issues = db.get_all_issues()
    issue_data = [_build_issue_row(i, tracking) for i in issues]
    issue_data.sort(key=lambda x: x["updated"], reverse=True)

    stats = _compute_stats(issue_data)

    index_html = _render_index(issue_data, stats)
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")

    for row in issue_data:
        issue = next((i for i in issues if i.issue_id == row["issue_id"]), None)
        if issue:
            report = _render_issue_report(row, issue, tracking)
            fname = f"{row['jira_key']}.html" if row.get("jira_key") else f"{row['issue_id']}.html"
            (issues_dir / fname).write_text(report, encoding="utf-8")

    tracking_data = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "issues": [{
            "issue_id": r["issue_id"],
            "jira_key": r["jira_key"],
            "summary": r["summary"],
            "status": r["status"],
            "status_category": r["status_category"],
            "qa_status": r["qa_status"],
            "qa_assignee": r["qa_assignee"],
            "internal_priority": r["internal_priority"],
        } for r in issue_data],
    }
    (output_dir / "issues.json").write_text(
        json.dumps(tracking_data, indent=2), encoding="utf-8"
    )

    return output_dir / "index.html"


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _build_issue_row(issue: ConsolidatedIssue, tracking: TrackingDB) -> dict[str, Any]:
    d = issue._data
    timeline = d.get("timeline", [])
    jstatus_entries = [e for e in timeline if e.get("type") == "jira_status"]
    last_status = jstatus_entries[-1] if jstatus_entries else {}

    jira_ids = d.get("jira_ids", [])
    jira_key = jira_ids[0] if jira_ids else ""

    trk = tracking.get(jira_key) if jira_key else tracking.get(issue.issue_id)

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
        "error_messages": d.get("error_messages", []),
        "stack_traces": d.get("stack_traces", []),
        "health_warnings": d.get("health_warnings", []),
        "qa_status": trk.get("qa_status", "not_assessed"),
        "qa_assignee": trk.get("qa_assignee", ""),
        "internal_priority": trk.get("internal_priority", ""),
        "analysis": trk.get("analysis", ""),
        "repro_steps": trk.get("repro_steps", ""),
        "test_coverage": trk.get("test_coverage", ""),
        "hotfix_status": trk.get("hotfix_status", ""),
        "notes": trk.get("notes", ""),
    }


def _compute_stats(rows: list[dict]) -> dict[str, Any]:
    total = len(rows)
    status_counts = Counter(r["status_category"] for r in rows)
    priority_counts = Counter(r["priority"] for r in rows)
    qa_counts = Counter(r["qa_status"] for r in rows)
    component_counts: Counter = Counter()
    label_counts: Counter = Counter()

    for r in rows:
        for c in r["components"]:
            component_counts[c] += 1
        for lbl in r["labels"]:
            label_counts[lbl] += 1

    open_count = status_counts.get("To Do", 0) + status_counts.get("In Progress", 0)

    return {
        "total": total,
        "open": open_count,
        "in_progress": status_counts.get("In Progress", 0),
        "closed": status_counts.get("Done", 0),
        "blocker": priority_counts.get("blocker", 0),
        "major": priority_counts.get("major", 0),
        "needs_analysis": qa_counts.get("not_assessed", 0) + qa_counts.get("needs_analysis", 0),
        "test_written": qa_counts.get("test_written", 0) + qa_counts.get("verified", 0),
        "status_counts": dict(status_counts.most_common()),
        "priority_counts": dict(priority_counts.most_common()),
        "qa_counts": {QA_STATUS_LABELS.get(k, k): v for k, v in qa_counts.most_common()},
        "component_counts": dict(component_counts.most_common(20)),
        "label_counts": dict(label_counts.most_common(15)),
    }


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    return html_lib.escape(str(text))


def _format_analysis(text: str) -> str:
    """Convert structured plain text to HTML with bullets, code, headings."""
    import re as _re
    lines = text.split("\n")
    out = []
    in_list = False
    in_pre = False

    for line in lines:
        stripped = line.strip()

        # Code block markers
        if stripped.startswith("```"):
            if in_list:
                out.append("</ul>")
                in_list = False
            if in_pre:
                out.append("</pre>")
                in_pre = False
            else:
                out.append("<pre>")
                in_pre = True
            continue

        if in_pre:
            out.append(_esc(line))
            continue

        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue

        # Section headers (ALL CAPS line or line ending with colon)
        if (_re.match(r'^[A-Z][A-Z &/\-]{3,}:?$', stripped) or
                _re.match(r'^[A-Z][A-Za-z &/\-]+:$', stripped)):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f'<div class="section-head">{_esc(stripped.rstrip(":"))}</div>')
            continue

        # SUMMARY: line at top
        if stripped.upper().startswith("SUMMARY:"):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f'<div class="summary-line">{_esc(stripped[8:].strip())}</div>')
            continue

        # Bullet lines (- or * or numbered)
        bullet_match = _re.match(r'^[\-\*]\s+(.+)', stripped)
        num_match = _re.match(r'^(\d+)[.)]\s+(.+)', stripped)

        if bullet_match or num_match:
            if not in_list:
                out.append("<ul>")
                in_list = True
            content = bullet_match.group(1) if bullet_match else num_match.group(2)
            prefix = ""
            if num_match:
                prefix = f'<span class="step-num">{num_match.group(1)}.</span> '
            # Wrap backtick-quoted text in <code>
            content = _re.sub(r'`([^`]+)`', lambda m: f'<code>{_esc(m.group(1))}</code>', _esc(content))
            # Wrap command-like text (ceph ..., radosgw-admin ..., etc.)
            content = _re.sub(
                r'(?<![<\w])((?:ceph|rados|rbd|rgw|radosgw-admin|cephadm|smbclient|fio|mount|curl|podman|systemctl|journalctl)\s[^<,]+)',
                lambda m: f'<code>{m.group(1).strip()}</code>',
                content
            )
            out.append(f"<li>{prefix}{content}</li>")
            continue

        # Regular text line
        if in_list:
            out.append("</ul>")
            in_list = False
        # Wrap backtick-quoted text
        escaped = _esc(stripped)
        escaped = _re.sub(r'`([^`]+)`', lambda m: f'<code>{m.group(1)}</code>', escaped)
        out.append(f"<div>{escaped}</div>")

    if in_list:
        out.append("</ul>")
    if in_pre:
        out.append("</pre>")

    return "\n".join(out)


def _status_badge(status: str, category: str) -> str:
    color_map = {"Done": "#198038", "In Progress": "#0f62fe", "To Do": "#da1e28"}
    color = color_map.get(category, "#525252")
    return (
        f'<span class="badge" style="white-space:nowrap;color:#fff;background:{color};">'
        f'{_esc(status)}</span>'
    )


def _priority_badge(priority: str) -> str:
    color_map = {"blocker": "#da1e28", "major": "#ff832b", "normal": "#0f62fe", "minor": "#198038"}
    color = color_map.get(priority.lower(), "#525252")
    return (
        f'<span class="badge-outline" style="white-space:nowrap;color:{color};'
        f'background:{color}18;border-color:{color}40;">{_esc(priority)}</span>'
    )


def _qa_badge(qa_status: str) -> str:
    color_map = {
        "not_assessed": "#8d8d8d",
        "needs_analysis": "#da1e28",
        "reproducing": "#ff832b",
        "test_written": "#0f62fe",
        "verified": "#198038",
        "wont_fix": "#525252",
    }
    color = color_map.get(qa_status, "#8d8d8d")
    label = QA_STATUS_LABELS.get(qa_status, qa_status)
    return (
        f'<span class="badge-outline" style="white-space:nowrap;color:{color};'
        f'background:{color}18;border-color:{color}40;">{_esc(label)}</span>'
    )


def _label_badges(labels: list[str]) -> str:
    parts = []
    highlight = {"Ceph_L3", "IBM_Customer_Issue", "Hotfix_requested", "Hotfix_Delivered", "Regression"}
    for lbl in labels:
        if lbl in highlight:
            color = "#8a3ffc" if lbl.startswith(("Ceph", "IBM")) else "#da1e28" if "Regression" in lbl else "#009d9a"
            parts.append(
                f'<span class="lbl-tag" style="color:{color};background:{color}12;'
                f'border-color:{color}30;">{_esc(lbl)}</span>'
            )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --ibm-blue: #0f62fe; --ibm-dark: #001d6c; --ibm-darker: #000a3d;
  --ibm-teal: #009d9a; --ibm-light-blue: #e8f0fe; --ibm-hover: #0353e9;
  --ibm-gray-10: #f4f4f4; --ibm-gray-20: #e0e0e0; --ibm-gray-30: #c6c6c6;
  --ibm-gray-50: #8d8d8d; --ibm-gray-70: #525252; --ibm-gray-90: #262626;
  --ibm-gray-100: #161616;
  --ibm-red: #da1e28; --ibm-green: #198038; --ibm-purple: #8a3ffc; --ibm-orange: #ff832b;
  --sidebar-width: 240px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'IBM Plex Sans', -apple-system, sans-serif; font-size: 14px; line-height: 1.5; color: var(--ibm-gray-90); background: #fff; }

.header {
  position: fixed; top: 0; left: 0; right: 0; height: 48px; z-index: 100;
  background: var(--ibm-dark); color: #fff;
  display: flex; align-items: center; padding: 0 1.5rem;
  box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
.header h1 { font-size: 0.95rem; font-weight: 600; letter-spacing: 0.02em; }
.header .gen-date { margin-left: auto; font-size: 0.75rem; opacity: 0.7; }

.sidebar {
  position: fixed; top: 48px; left: 0; bottom: 0; width: var(--sidebar-width);
  background: var(--ibm-gray-10); border-right: 1px solid var(--ibm-gray-20);
  overflow-y: auto; padding: 1.5rem 0; z-index: 90;
}
.sidebar nav a {
  display: block; padding: 0.5rem 1.5rem; color: var(--ibm-gray-70);
  text-decoration: none; font-size: 0.82rem; border-left: 3px solid transparent;
  transition: all 0.15s ease;
}
.sidebar nav a:hover { color: var(--ibm-blue); background: rgba(15,98,254,0.04); }
.sidebar nav a.active {
  color: var(--ibm-blue); font-weight: 600;
  border-left-color: var(--ibm-blue); background: rgba(15,98,254,0.06);
}

.main {
  margin-left: var(--sidebar-width); margin-top: 48px;
  padding: 2rem 2.5rem; max-width: 1600px;
}

.metrics-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 0.75rem; margin: 1.5rem 0;
}
.metric-card {
  background: var(--ibm-gray-10); border-radius: 6px; padding: 1rem;
  text-align: center; border-top: 3px solid var(--ibm-blue);
}
.metric-card.red { border-top-color: var(--ibm-red); }
.metric-card.green { border-top-color: var(--ibm-green); }
.metric-card.orange { border-top-color: var(--ibm-orange); }
.metric-card.purple { border-top-color: var(--ibm-purple); }
.metric-card.teal { border-top-color: var(--ibm-teal); }
.metric-value { font-size: 1.8rem; font-weight: 700; color: var(--ibm-blue); }
.metric-card.red .metric-value { color: var(--ibm-red); }
.metric-card.green .metric-value { color: var(--ibm-green); }
.metric-card.orange .metric-value { color: var(--ibm-orange); }
.metric-card.purple .metric-value { color: var(--ibm-purple); }
.metric-card.teal .metric-value { color: var(--ibm-teal); }
.metric-label { font-size: 0.72rem; color: var(--ibm-gray-70); margin-top: 0.2rem; text-transform: uppercase; letter-spacing: 0.05em; }

.charts-row {
  display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin: 1.5rem 0;
}
.chart-container {
  background: var(--ibm-gray-10); border-radius: 6px; padding: 1.2rem;
  height: 300px; position: relative;
}
.chart-title { font-size: 0.82rem; font-weight: 600; color: var(--ibm-gray-70); margin-bottom: 0.5rem; text-transform: uppercase; letter-spacing: 0.04em; }

.filter-bar {
  margin: 1.5rem 0 0.75rem; display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;
}
.filter-bar input[type="text"] {
  font-family: inherit; font-size: 0.8rem; padding: 0.35rem 0.7rem;
  border: 1px solid var(--ibm-gray-30); border-radius: 4px; background: #fff;
  color: var(--ibm-gray-90); min-width: 180px;
}
.filter-bar input:focus { outline: 2px solid var(--ibm-blue); border-color: transparent; }
.filter-count { font-size: 0.82rem; color: var(--ibm-gray-50); margin-left: auto; }
.not-toggle {
  display: inline-flex; align-items: center; gap: 0.3rem; font-size: 0.75rem;
  color: var(--ibm-gray-70); cursor: pointer; user-select: none;
  padding: 0.25rem 0.6rem; border-radius: 4px; border: 1px solid var(--ibm-gray-30);
  background: #fff; transition: all 0.15s;
}
.not-toggle.active { background: var(--ibm-red); color: #fff; border-color: var(--ibm-red); }
.filter-group { margin-bottom: 0.5rem; }
.filter-group-label {
  font-size: 0.68rem; font-weight: 600; color: var(--ibm-gray-50);
  text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 0.3rem;
}
.chip-row { display: flex; flex-wrap: wrap; gap: 0.3rem; }
.chip {
  display: inline-block; padding: 3px 10px; border-radius: 14px; font-size: 0.72rem;
  font-weight: 500; cursor: pointer; user-select: none; transition: all 0.12s;
  border: 1px solid var(--ibm-gray-30); background: #fff; color: var(--ibm-gray-70);
}
.chip:hover { border-color: var(--ibm-blue); color: var(--ibm-blue); }
.chip.selected { background: var(--ibm-blue); color: #fff; border-color: var(--ibm-blue); }
.chip.selected.not-mode { background: var(--ibm-red); border-color: var(--ibm-red); }
.filters-panel {
  background: var(--ibm-gray-10); border-radius: 6px; padding: 0.8rem 1rem;
  margin-bottom: 1rem; display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 0.75rem;
}
@media (max-width: 1000px) { .filters-panel { grid-template-columns: 1fr 1fr; } }

table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
th {
  background: var(--ibm-gray-10); font-weight: 600; text-align: left;
  padding: 0.5rem 0.6rem; border-bottom: 2px solid var(--ibm-gray-20);
  position: sticky; top: 0; cursor: pointer; white-space: nowrap;
  user-select: none; font-size: 0.75rem;
}
th:hover { color: var(--ibm-blue); }
td { padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--ibm-gray-20); vertical-align: middle; }
tr:hover { background: rgba(15,98,254,0.02); }

.badge {
  display: inline-block; padding: 2px 8px; border-radius: 10px;
  font-size: 0.72rem; font-weight: 500; white-space: nowrap; line-height: 1.4;
}
.badge-outline {
  display: inline-block; padding: 1px 7px; border-radius: 4px;
  font-size: 0.7rem; font-weight: 500; white-space: nowrap; line-height: 1.4;
  border: 1px solid;
}
.lbl-tag {
  display: inline-block; padding: 1px 5px; border-radius: 3px;
  font-size: 0.68rem; border: 1px solid; margin: 0 1px; white-space: nowrap;
}

section { scroll-margin-top: 70px; }
h2 { font-size: 1.1rem; font-weight: 600; color: var(--ibm-gray-90); margin: 2rem 0 0.75rem; padding-bottom: 0.4rem; border-bottom: 1px solid var(--ibm-gray-20); }

@media (max-width: 1000px) {
  .sidebar { display: none; }
  .main { margin-left: 0; }
  .charts-row { grid-template-columns: 1fr; }
}
"""


def _render_index(rows: list[dict], stats: dict) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    rows_json = json.dumps(rows, default=str)
    stats_json = json.dumps(stats, default=str)

    issue_rows_html = []
    for r in rows:
        comp_str = ", ".join(r["components"][:2])
        updated_short = r["updated"][:10] if r["updated"] else ""
        issue_rows_html.append(f"""<tr class="issue-row"
          data-status="{_esc(r['status_category'])}"
          data-priority="{_esc(r['priority'])}"
          data-component="{_esc(','.join(r['components']))}"
          data-labels="{_esc(','.join(r['labels']))}"
          data-qa="{_esc(r['qa_status'])}">
        <td><a href="issues/{r['jira_key'] if r.get('jira_key') else r['issue_id']}.html" class="key-link">{_esc(r['jira_key'])}</a></td>
        <td class="summary-cell" title="{_esc(r['summary'])}">{_esc(r['summary'][:90])}</td>
        <td>{_status_badge(r['status'], r['status_category'])}</td>
        <td>{_priority_badge(r['priority'])}</td>
        <td>{_qa_badge(r['qa_status'])}</td>
        <td class="sm">{_esc(comp_str)}</td>
        <td class="sm">{_esc(r['assignee'])}</td>
        <td class="sm">{_esc(r['qa_assignee'])}</td>
        <td class="sm">{_label_badges(r['labels'])}</td>
        <td class="sm date">{updated_short}</td>
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
{_CSS}
.key-link {{ color: var(--ibm-blue); text-decoration: none; font-weight: 500; white-space: nowrap; }}
.key-link:hover {{ text-decoration: underline; }}
.summary-cell {{ max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.sm {{ font-size: 0.78rem; }}
.date {{ color: var(--ibm-gray-50); white-space: nowrap; }}
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
  <div style="background:linear-gradient(135deg, var(--ibm-dark) 0%, #002d9c 100%); color:#fff; border-radius:8px; padding:1.8rem 2.2rem; margin-bottom:1.5rem;">
    <div style="font-size:0.95rem;font-weight:300;opacity:0.85;">Ceph Prio-Hub &mdash; Customer Escalation Tracker</div>
    <div style="font-size:1.4rem;font-weight:600;margin-top:0.4rem;">
      <span style="color:#78a9ff;">{stats['total']}</span> tracked issues &mdash;
      <span style="color:#78a9ff;">{stats['open']}</span> open,
      <span style="color:#78a9ff;">{stats['closed']}</span> resolved
    </div>
    <div style="margin-top:0.5rem;font-size:0.85rem;opacity:0.9;">
      {stats['needs_analysis']} issues need QA analysis &bull;
      {stats['test_written']} QA verified &bull;
      {stats['blocker']} blockers &bull; {stats['major']} major
    </div>
  </div>

  <div class="metrics-grid">
    <div class="metric-card"><div class="metric-value">{stats['total']}</div><div class="metric-label">Total</div></div>
    <div class="metric-card red"><div class="metric-value">{stats['open']}</div><div class="metric-label">Open</div></div>
    <div class="metric-card"><div class="metric-value">{stats['in_progress']}</div><div class="metric-label">In Progress</div></div>
    <div class="metric-card green"><div class="metric-value">{stats['closed']}</div><div class="metric-label">Resolved</div></div>
    <div class="metric-card orange"><div class="metric-value">{stats['needs_analysis']}</div><div class="metric-label">Needs QA</div></div>
    <div class="metric-card teal"><div class="metric-value">{stats['test_written']}</div><div class="metric-label">QA Verified</div></div>
    <div class="metric-card purple"><div class="metric-value">{stats['blocker']}</div><div class="metric-label">Blockers</div></div>
    <div class="metric-card"><div class="metric-value">{stats['major']}</div><div class="metric-label">Major</div></div>
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
      <div class="chart-title">QA Coverage Status</div>
      <canvas id="chart-qa"></canvas>
    </div>
  </div>
  <div class="charts-row">
    <div class="chart-container">
      <div class="chart-title">JIRA Status Distribution</div>
      <canvas id="chart-status"></canvas>
    </div>
    <div class="chart-container">
      <div class="chart-title">Priority Distribution</div>
      <canvas id="chart-priority"></canvas>
    </div>
  </div>
</section>

<section id="issues">
  <h2>All Issues</h2>

  <div class="filter-bar">
    <span class="not-toggle" id="not-toggle" title="Toggle NOT mode: exclude selected values instead of including">
      <span style="font-size:0.85rem;">&#8800;</span> NOT
    </span>
    <input type="text" id="filter-search" placeholder="Search issues...">
    <span class="filter-count" id="visible-count">{stats['total']} issues</span>
  </div>
  <div class="filters-panel">
    <div class="filter-group">
      <div class="filter-group-label">Status</div>
      <div class="chip-row" id="chips-status">
        <span class="chip" data-val="To Do">Open</span>
        <span class="chip" data-val="In Progress">In Progress</span>
        <span class="chip" data-val="Done">Resolved</span>
      </div>
    </div>
    <div class="filter-group">
      <div class="filter-group-label">Priority</div>
      <div class="chip-row" id="chips-priority">
        <span class="chip" data-val="blocker">Blocker</span>
        <span class="chip" data-val="major">Major</span>
        <span class="chip" data-val="normal">Normal</span>
        <span class="chip" data-val="minor">Minor</span>
      </div>
    </div>
    <div class="filter-group">
      <div class="filter-group-label">QA Status</div>
      <div class="chip-row" id="chips-qa">
        <span class="chip" data-val="not_assessed">Not Assessed</span>
        <span class="chip" data-val="needs_analysis">Needs Analysis</span>
        <span class="chip" data-val="reproducing">Reproducing</span>
        <span class="chip" data-val="test_written">Test Written</span>
        <span class="chip" data-val="verified">Verified</span>
        <span class="chip" data-val="wont_fix">Won't Fix</span>
      </div>
    </div>
    <div class="filter-group">
      <div class="filter-group-label">Component</div>
      <div class="chip-row" id="chips-component"></div>
    </div>
  </div>

  <table id="issue-table">
    <thead>
      <tr>
        <th data-sort="key">Key</th>
        <th data-sort="summary">Summary</th>
        <th data-sort="status">Status</th>
        <th data-sort="priority">Priority</th>
        <th data-sort="qa">QA Status</th>
        <th data-sort="component">Components</th>
        <th data-sort="assignee">Assignee</th>
        <th data-sort="qa_assignee">QA Owner</th>
        <th>Labels</th>
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

const IBM_COLORS = ['#0f62fe','#009d9a','#8a3ffc','#ff832b','#da1e28','#198038','#002d9c','#b28600','#6929c4','#1192e8','#fa4d56','#005d5d'];

function makeChart(id, type, labels, data, opts) {{
  const ctx = document.getElementById(id);
  if (!ctx) return;
  new Chart(ctx, {{
    type, data: {{
      labels,
      datasets: [{{ data, backgroundColor: opts.colors || IBM_COLORS.slice(0, data.length), borderWidth: 0, borderRadius: type === 'bar' ? 4 : 0 }}]
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

makeChart('chart-components', 'bar',
  Object.keys(STATS.component_counts).slice(0, 12),
  Object.keys(STATS.component_counts).slice(0, 12).map(k => STATS.component_counts[k]), {{}});

makeChart('chart-qa', 'doughnut',
  Object.keys(STATS.qa_counts),
  Object.values(STATS.qa_counts),
  {{ colors: ['#8d8d8d','#da1e28','#ff832b','#0f62fe','#198038','#525252'] }});

makeChart('chart-status', 'doughnut',
  Object.keys(STATS.status_counts),
  Object.values(STATS.status_counts),
  {{ colors: ['#da1e28','#0f62fe','#198038'] }});

makeChart('chart-priority', 'doughnut',
  Object.keys(STATS.priority_counts),
  Object.values(STATS.priority_counts),
  {{ colors: ['#0f62fe','#ff832b','#da1e28','#198038'] }});

// Multi-select chip filters
const selections = {{ status: new Set(), priority: new Set(), qa: new Set(), component: new Set() }};
let notMode = false;

// Populate component chips
const compSet = new Set();
ISSUES.forEach(i => (i.components || []).forEach(c => compSet.add(c)));
const compRow = document.getElementById('chips-component');
[...compSet].sort().forEach(c => {{
  const sp = document.createElement('span');
  sp.className = 'chip'; sp.dataset.val = c; sp.textContent = c;
  compRow.appendChild(sp);
}});

// NOT toggle
document.getElementById('not-toggle').addEventListener('click', function() {{
  notMode = !notMode;
  this.classList.toggle('active', notMode);
  document.querySelectorAll('.chip.selected').forEach(ch => ch.classList.toggle('not-mode', notMode));
  applyFilters();
}});

// Chip click handlers
document.querySelectorAll('.chip-row').forEach(row => {{
  const group = row.id.replace('chips-', '');
  row.addEventListener('click', e => {{
    const chip = e.target.closest('.chip');
    if (!chip) return;
    const val = chip.dataset.val;
    if (selections[group].has(val)) {{
      selections[group].delete(val);
      chip.classList.remove('selected', 'not-mode');
    }} else {{
      selections[group].add(val);
      chip.classList.add('selected');
      if (notMode) chip.classList.add('not-mode');
    }}
    applyFilters();
  }});
}});

// Filtering
function matchFilter(set, value, isNot) {{
  if (set.size === 0) return true;
  const has = set.has(value);
  return isNot ? !has : has;
}}

function matchFilterMulti(set, values, isNot) {{
  if (set.size === 0) return true;
  const has = values.some(v => set.has(v));
  return isNot ? !has : has;
}}

function applyFilters() {{
  const t = document.getElementById('filter-search').value.toLowerCase();
  let vis = 0;
  document.querySelectorAll('.issue-row').forEach(row => {{
    let show = true;
    if (!matchFilter(selections.status, row.dataset.status, notMode)) show = false;
    if (!matchFilter(selections.priority, row.dataset.priority, notMode)) show = false;
    if (!matchFilter(selections.qa, row.dataset.qa, notMode)) show = false;
    const comps = row.dataset.component ? row.dataset.component.split(',') : [];
    if (!matchFilterMulti(selections.component, comps, notMode)) show = false;
    if (t && !row.children[1].textContent.toLowerCase().includes(t)) show = false;
    row.style.display = show ? '' : 'none';
    if (show) vis++;
  }});
  const activeFilters = Object.values(selections).some(s => s.size > 0);
  const prefix = activeFilters ? (notMode ? 'Excluding \\u2192 ' : 'Showing ') : '';
  document.getElementById('visible-count').textContent = prefix + vis + ' issues';
}}
document.getElementById('filter-search').addEventListener('input', applyFilters);

// Column sorting
let sortCol = '', sortAsc = true;
document.querySelectorAll('th[data-sort]').forEach(th => {{
  th.addEventListener('click', () => {{
    const col = th.dataset.sort;
    if (sortCol === col) sortAsc = !sortAsc; else {{ sortCol = col; sortAsc = true; }}
    const tbody = document.querySelector('#issue-table tbody');
    const rows = [...tbody.querySelectorAll('tr')];
    const idx = [...th.parentNode.children].indexOf(th);
    rows.sort((a, b) => {{
      const aT = a.children[idx].textContent.trim(), bT = b.children[idx].textContent.trim();
      return sortAsc ? aT.localeCompare(bT) : bT.localeCompare(aT);
    }});
    rows.forEach(r => tbody.appendChild(r));
  }});
}});

// Scroll spy
const sections = document.querySelectorAll('section[id]');
const navLinks = document.querySelectorAll('.sidebar nav a');
window.addEventListener('scroll', () => {{
  let cur = '';
  sections.forEach(s => {{ if (window.scrollY >= s.offsetTop - 100) cur = s.id; }});
  navLinks.forEach(l => l.classList.toggle('active', l.getAttribute('href') === '#' + cur));
}});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Per-issue report
# ---------------------------------------------------------------------------

_ISSUE_CSS = """
:root {
  --ibm-blue: #0f62fe; --ibm-dark: #001d6c; --ibm-teal: #009d9a;
  --ibm-gray-10: #f4f4f4; --ibm-gray-20: #e0e0e0; --ibm-gray-50: #8d8d8d;
  --ibm-gray-70: #525252; --ibm-gray-90: #262626; --ibm-gray-100: #161616;
  --ibm-red: #da1e28; --ibm-green: #198038; --ibm-purple: #8a3ffc; --ibm-orange: #ff832b;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'IBM Plex Sans', sans-serif; font-size: 14px; line-height: 1.6; color: var(--ibm-gray-90); background: #fff; padding: 2rem; max-width: 960px; margin: 0 auto; }
a.back { font-size: 0.85rem; color: var(--ibm-blue); text-decoration: none; }
a.back:hover { text-decoration: underline; }
h1 { font-size: 1.25rem; font-weight: 600; margin: 0.75rem 0 0.5rem; }
h2 { font-size: 1rem; font-weight: 600; margin: 2rem 0 0.5rem; padding-bottom: 0.25rem; border-bottom: 1px solid var(--ibm-gray-20); }
h3 { font-size: 0.9rem; font-weight: 600; margin: 1.2rem 0 0.4rem; color: var(--ibm-gray-70); }

.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.72rem; font-weight: 500; white-space: nowrap; }
.badge-outline { display: inline-block; padding: 1px 7px; border-radius: 4px; font-size: 0.7rem; font-weight: 500; white-space: nowrap; border: 1px solid; }

.meta-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; margin: 1rem 0; }
.meta-card { background: var(--ibm-gray-10); border-radius: 6px; padding: 0.9rem 1rem; }
.meta-card dt { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ibm-gray-50); font-weight: 600; margin-top: 0.5rem; }
.meta-card dt:first-child { margin-top: 0; }
.meta-card dd { font-size: 0.88rem; margin-top: 0.1rem; }

.qa-section {
  background: var(--ibm-light-blue); border: 1px solid rgba(15,98,254,0.15);
  border-radius: 8px; padding: 1.2rem 1.5rem; margin: 1.5rem 0;
}
.qa-section h3 { color: var(--ibm-blue); margin: 0 0 0.75rem; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.06em; }
.qa-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.5rem; margin-bottom: 0.75rem; }
.qa-field dt { font-size: 0.7rem; text-transform: uppercase; color: var(--ibm-gray-50); font-weight: 600; }
.qa-field dd { font-size: 0.88rem; margin-top: 0.1rem; }

.analysis-box {
  background: #fff; border: 1px solid var(--ibm-gray-20); border-radius: 6px;
  padding: 1rem 1.2rem; margin: 0.75rem 0; min-height: 60px;
  font-size: 0.85rem; line-height: 1.6;
}
.analysis-box.empty { color: var(--ibm-gray-50); font-style: italic; font-size: 0.85rem; }
.analysis-label { font-size: 0.75rem; font-weight: 600; color: var(--ibm-gray-70); text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 0.3rem; }
.analysis-box .summary-line { font-weight: 600; color: var(--ibm-gray-90); margin-bottom: 0.6rem; font-size: 0.88rem; }
.analysis-box ul { margin: 0.3rem 0 0.6rem 1.2rem; padding: 0; }
.analysis-box li { margin-bottom: 0.25rem; }
.analysis-box .section-head { font-weight: 600; color: var(--ibm-gray-70); font-size: 0.8rem; margin-top: 0.8rem; margin-bottom: 0.2rem; text-transform: uppercase; letter-spacing: 0.03em; }
.analysis-box code {
  background: var(--ibm-gray-10); padding: 1px 5px; border-radius: 3px;
  font-family: 'IBM Plex Mono', monospace; font-size: 0.82em; color: var(--ibm-dark);
}
.analysis-box pre {
  background: var(--ibm-gray-100); color: #f4f4f4; padding: 0.6rem 0.8rem;
  border-radius: 4px; font-family: 'IBM Plex Mono', monospace; font-size: 0.8rem;
  overflow-x: auto; margin: 0.3rem 0 0.5rem; white-space: pre-wrap;
}
.analysis-box .step-num { display: inline-block; min-width: 1.4em; font-weight: 600; color: var(--ibm-blue); }

.error-box {
  background: var(--ibm-gray-100); color: #f4f4f4; padding: 0.8rem 1rem;
  border-radius: 6px; font-family: 'IBM Plex Mono', monospace; font-size: 0.8rem;
  overflow-x: auto; margin: 0.5rem 0; white-space: pre-wrap; word-break: break-all;
}

.label-list span {
  display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 0.75rem;
  background: var(--ibm-gray-10); border: 1px solid var(--ibm-gray-20); margin: 2px;
}

.tl-entry { padding: 0.5rem 0; border-bottom: 1px solid var(--ibm-gray-20); font-size: 0.82rem; }
.tl-date { font-family: 'IBM Plex Mono', monospace; font-size: 0.75rem; color: var(--ibm-gray-50); margin-right: 0.4rem; }
.tl-badge {
  display: inline-block; padding: 1px 7px; border-radius: 3px; font-size: 0.68rem;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; margin-right: 0.3rem;
}
.tl-badge.status { background: #e8f0fe; color: var(--ibm-blue); }
.tl-badge.comment { background: #e6f4ea; color: var(--ibm-green); }
.tl-badge.email { background: #f3e8fd; color: var(--ibm-purple); }

.pencil-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border: none; background: transparent;
  cursor: pointer; border-radius: 3px; vertical-align: middle;
  opacity: 0.35; transition: all 0.15s; margin-left: 0.3rem; padding: 0;
}
.pencil-btn:hover { opacity: 1; background: rgba(15,98,254,0.08); }
.pencil-btn svg { width: 13px; height: 13px; fill: var(--ibm-blue); }

.inline-edit { display: none; margin-top: 0.3rem; }
.inline-edit.active { display: block; }
.inline-edit select, .inline-edit textarea {
  font-family: inherit; font-size: 0.82rem; padding: 0.3rem 0.5rem;
  border: 1px solid var(--ibm-blue); border-radius: 4px; background: #fff;
  color: var(--ibm-gray-90); width: 100%; box-sizing: border-box;
}
.inline-edit select:focus, .inline-edit textarea:focus { outline: 2px solid var(--ibm-blue); border-color: transparent; }
.inline-edit select { max-width: 200px; }

.field-view { display: inline; }
.field-view.hidden { display: none; }

.save-bar {
  position: fixed; bottom: 0; left: 0; right: 0; z-index: 100;
  background: #fff; border-top: 2px solid var(--ibm-blue);
  box-shadow: 0 -2px 8px rgba(0,0,0,0.08);
  padding: 0.6rem 2rem; display: none; align-items: center; gap: 0.75rem;
}
.save-bar.visible { display: flex; }
.save-bar .save-btn {
  font-size: 0.8rem; padding: 0.4rem 1.2rem; border-radius: 4px; border: none;
  background: var(--ibm-blue); color: #fff; cursor: pointer; font-weight: 500;
}
.save-bar .save-btn:hover { background: #0353e9; }
.save-bar .discard-btn {
  font-size: 0.8rem; padding: 0.4rem 1rem; border-radius: 4px;
  border: 1px solid var(--ibm-gray-30); background: #fff; color: var(--ibm-gray-70);
  cursor: pointer; font-weight: 500;
}
.save-bar .discard-btn:hover { background: var(--ibm-gray-10); }
.save-bar .change-count { font-size: 0.78rem; color: var(--ibm-gray-50); }

.settings-gear {
  position: fixed; top: 12px; right: 16px; z-index: 200;
  width: 30px; height: 30px; border: none; background: transparent;
  cursor: pointer; opacity: 0.4; transition: opacity 0.15s; padding: 0;
}
.settings-gear:hover { opacity: 0.9; }
.settings-gear svg { width: 22px; height: 22px; fill: var(--ibm-gray-70); }
.settings-gear.configured svg { fill: var(--ibm-green); }

.settings-panel {
  display: none; position: fixed; top: 50px; right: 16px; z-index: 200;
  background: #fff; border: 1px solid var(--ibm-gray-20); border-radius: 8px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12); padding: 1.2rem 1.4rem;
  width: 360px; font-size: 0.82rem;
}
.settings-panel.open { display: block; }
.settings-panel h4 { font-size: 0.82rem; font-weight: 600; margin-bottom: 0.6rem; color: var(--ibm-gray-90); }
.settings-panel p { font-size: 0.75rem; color: var(--ibm-gray-50); margin-bottom: 0.6rem; line-height: 1.4; }
.settings-panel input[type="password"] {
  font-family: 'IBM Plex Mono', monospace; font-size: 0.78rem; padding: 0.35rem 0.5rem;
  border: 1px solid var(--ibm-gray-30); border-radius: 4px; width: 100%;
  box-sizing: border-box; margin-bottom: 0.5rem;
}
.settings-panel input:focus { outline: 2px solid var(--ibm-blue); border-color: transparent; }
.settings-panel .btn-row { display: flex; gap: 0.4rem; align-items: center; }
.settings-panel .btn-sm {
  font-size: 0.75rem; padding: 0.3rem 0.8rem; border-radius: 4px; cursor: pointer;
  font-weight: 500; border: none;
}
.settings-panel .btn-sm.primary { background: var(--ibm-blue); color: #fff; }
.settings-panel .btn-sm.danger { background: var(--ibm-red); color: #fff; }
.settings-panel .btn-sm.secondary { background: var(--ibm-gray-10); color: var(--ibm-gray-70); border: 1px solid var(--ibm-gray-30); }
.sync-indicator {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  margin-right: 0.3rem; vertical-align: middle;
}
.sync-indicator.on { background: var(--ibm-green); }
.sync-indicator.off { background: var(--ibm-gray-50); }
"""


def _render_issue_report(row: dict, issue: ConsolidatedIssue, tracking: TrackingDB) -> str:
    d = issue._data
    timeline = d.get("timeline", [])
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # Timeline
    tl_parts = []
    for entry in timeline:
        etype = entry.get("type", "")
        edate = entry.get("date", "")[:19].replace("T", " ")
        if etype == "jira_status":
            tl_parts.append(
                f'<div class="tl-entry"><span class="tl-date">{_esc(edate)}</span>'
                f'<span class="tl-badge status">Status</span> '
                f'{_esc(entry.get("status", ""))} '
                f'<span style="color:var(--ibm-gray-50);">({_esc(entry.get("assignee", ""))})</span></div>'
            )
        elif etype == "jira_comment":
            body = sanitize_text(entry.get("summary", ""))
            tl_parts.append(
                f'<div class="tl-entry"><span class="tl-date">{_esc(edate)}</span>'
                f'<span class="tl-badge comment">Comment</span> '
                f'<strong>{_esc(entry.get("author", ""))}</strong>: '
                f'{_esc(body[:250])}</div>'
            )
        elif etype == "email":
            tl_parts.append(
                f'<div class="tl-entry"><span class="tl-date">{_esc(edate)}</span>'
                f'<span class="tl-badge email">Email</span> '
                f'{_esc(entry.get("from", ""))}: {_esc(entry.get("subject", "")[:100])}</div>'
            )

    timeline_html = "\n".join(tl_parts) or '<p style="color:var(--ibm-gray-50);">No timeline entries.</p>'

    jira_link = (
        f'<a href="{_esc(row["jira_url"])}" target="_blank" style="color:var(--ibm-blue);">'
        f'{_esc(row["jira_key"])}</a>'
    ) if row["jira_url"] else "N/A"

    # Error messages / stack traces
    errors_html = ""
    errs = row.get("error_messages", [])
    traces = row.get("stack_traces", [])
    warnings = row.get("health_warnings", [])
    if errs or traces or warnings:
        errors_html += '<h2>Error Signals</h2>'
        if errs:
            errors_html += '<h3>Error Messages</h3>'
            for e in errs[:5]:
                errors_html += f'<div class="error-box">{_esc(e[:500])}</div>'
        if traces:
            errors_html += '<h3>Stack Traces</h3>'
            for t in traces[:3]:
                errors_html += f'<div class="error-box">{_esc(t[:1000])}</div>'
        if warnings:
            errors_html += '<h3>Health Warnings</h3>'
            for w in warnings[:5]:
                errors_html += f'<div class="error-box">{_esc(w[:300])}</div>'

    # Analysis fields
    def _analysis_block(label: str, content: str) -> str:
        if content:
            return (
                f'<div class="analysis-label">{_esc(label)}</div>'
                f'<div class="analysis-box">{_format_analysis(content)}</div>'
            )
        return (
            f'<div class="analysis-label">{_esc(label)}</div>'
            f'<div class="analysis-box empty">Not yet documented. '
            f'Edit tracking.json to add {label.lower()}.</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(row['jira_key'])} - {_esc(row['summary'][:60])}</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{_ISSUE_CSS}</style>
</head>
<body>
<a href="../index.html" class="back">&larr; Back to Dashboard</a>
<h1>{_esc(row['jira_key'])}: {_esc(row['summary'])}</h1>
<p style="font-size:0.8rem;color:var(--ibm-gray-50);">Generated: {now}</p>

<div class="meta-grid">
  <div class="meta-card"><dl>
    <dt>JIRA</dt><dd>{jira_link}</dd>
    <dt>Status</dt><dd>{_status_badge(row['status'], row['status_category'])}</dd>
    <dt>Priority</dt><dd>{_priority_badge(row['priority'])}</dd>
    <dt>Assignee</dt><dd>{_esc(row['assignee']) or 'Unassigned'}</dd>
  </dl></div>
  <div class="meta-card"><dl>
    <dt>Components</dt><dd>{_esc(', '.join(row['components']))}</dd>
    <dt>Versions</dt><dd>{_esc(', '.join(row['versions'])) or 'N/A'}</dd>
    <dt>Created</dt><dd>{_esc(row['created'][:10])}</dd>
    <dt>Updated</dt><dd>{_esc(row['updated'][:10])}</dd>
  </dl></div>
</div>

<div style="margin:0.5rem 0;">
  <strong style="font-size:0.78rem;color:var(--ibm-gray-70);">Labels:</strong>
  <div class="label-list">{''.join(f'<span>{_esc(l)}</span>' for l in row['labels'])}</div>
</div>

<div class="qa-section" id="qa-section">
  <h3>QA Tracking</h3>
  <div class="qa-grid">
    <div class="qa-field">
      <dt>QA Status <button class="pencil-btn" onclick="editField('qa_status')"><svg viewBox="0 0 16 16"><path d="M12.1 1.3a1 1 0 011.4 0l1.2 1.2a1 1 0 010 1.4L5.8 12.8l-3.3.8.8-3.3L12.1 1.3zM2 14h12v1H2v-1z"/></svg></button></dt>
      <dd>
        <span class="field-view" id="view-qa_status">{_qa_badge(row['qa_status'])}</span>
        <div class="inline-edit" id="edit-qa_status">
          <select id="ed-qa-status" onchange="markDirty()">
            {''.join(f'<option value="{s}"{" selected" if s == row["qa_status"] else ""}>{QA_STATUS_LABELS.get(s, s)}</option>' for s in QA_STATUSES)}
          </select>
        </div>
      </dd>
    </div>
    <div class="qa-field">
      <dt>QA Owner <button class="pencil-btn" onclick="editField('qa_assignee')"><svg viewBox="0 0 16 16"><path d="M12.1 1.3a1 1 0 011.4 0l1.2 1.2a1 1 0 010 1.4L5.8 12.8l-3.3.8.8-3.3L12.1 1.3zM2 14h12v1H2v-1z"/></svg></button></dt>
      <dd>
        <span class="field-view" id="view-qa_assignee">{_esc(row['qa_assignee']) or '<span style="color:var(--ibm-gray-50);">Unassigned</span>'}</span>
        <div class="inline-edit" id="edit-qa_assignee">
          <select id="ed-qa-assignee" onchange="markDirty()">
            <option value="">-- Unassigned --</option>
            {''.join(f'<option value="{_esc(n)}"{" selected" if n == row["qa_assignee"] else ""}>{_esc(n)}</option>' for n in TEAM_NAMES)}
          </select>
        </div>
      </dd>
    </div>
    <div class="qa-field">
      <dt>Internal Priority <button class="pencil-btn" onclick="editField('priority')"><svg viewBox="0 0 16 16"><path d="M12.1 1.3a1 1 0 011.4 0l1.2 1.2a1 1 0 010 1.4L5.8 12.8l-3.3.8.8-3.3L12.1 1.3zM2 14h12v1H2v-1z"/></svg></button></dt>
      <dd>
        <span class="field-view" id="view-priority">{_esc(row['internal_priority']) or '<span style="color:var(--ibm-gray-50);">--</span>'}</span>
        <div class="inline-edit" id="edit-priority">
          <select id="ed-priority" onchange="markDirty()">
            {''.join(f'<option value="{p}"{" selected" if p == row["internal_priority"] else ""}>{p or "-- None --"}</option>' for p in ["", "P0", "P1", "P2", "P3"])}
          </select>
        </div>
      </dd>
    </div>
  </div>
  <div class="qa-grid">
    <div class="qa-field">
      <dt>Hotfix Status <button class="pencil-btn" onclick="editField('hotfix')"><svg viewBox="0 0 16 16"><path d="M12.1 1.3a1 1 0 011.4 0l1.2 1.2a1 1 0 010 1.4L5.8 12.8l-3.3.8.8-3.3L12.1 1.3zM2 14h12v1H2v-1z"/></svg></button></dt>
      <dd>
        <span class="field-view" id="view-hotfix">{_esc(row['hotfix_status']) or '<span style="color:var(--ibm-gray-50);">--</span>'}</span>
        <div class="inline-edit" id="edit-hotfix">
          <select id="ed-hotfix" onchange="markDirty()">
            {''.join(f'<option value="{h}"{" selected" if h == row["hotfix_status"] else ""}>{h or "-- None --"}</option>' for h in ["", "requested", "delivered", "not_needed"])}
          </select>
        </div>
      </dd>
    </div>
    <div class="qa-field" style="grid-column: span 2;">
      <dt>Notes <button class="pencil-btn" onclick="editField('notes')"><svg viewBox="0 0 16 16"><path d="M12.1 1.3a1 1 0 011.4 0l1.2 1.2a1 1 0 010 1.4L5.8 12.8l-3.3.8.8-3.3L12.1 1.3zM2 14h12v1H2v-1z"/></svg></button></dt>
      <dd>
        <span class="field-view" id="view-notes">{_esc(row.get('notes', '')) or '<span style="color:var(--ibm-gray-50);">--</span>'}</span>
        <div class="inline-edit" id="edit-notes">
          <textarea id="ed-notes" rows="2" onchange="markDirty()" oninput="markDirty()">{_esc(row.get('notes', ''))}</textarea>
        </div>
      </dd>
    </div>
  </div>
</div>

<div>
  <div style="display:flex;align-items:center;gap:0.3rem;">
    <div class="analysis-label">Root Cause Analysis</div>
    <button class="pencil-btn" onclick="editField('analysis')"><svg viewBox="0 0 16 16"><path d="M12.1 1.3a1 1 0 011.4 0l1.2 1.2a1 1 0 010 1.4L5.8 12.8l-3.3.8.8-3.3L12.1 1.3zM2 14h12v1H2v-1z"/></svg></button>
  </div>
  <span class="field-view" id="view-analysis">
    <div class="analysis-box{' empty' if not row.get('analysis') else ''}">{_format_analysis(row.get('analysis', '')) if row.get('analysis') else 'Not yet documented.'}</div>
  </span>
  <div class="inline-edit" id="edit-analysis">
    <textarea id="ed-analysis" rows="10" style="width:100%;font-family:'IBM Plex Mono',monospace;font-size:0.82rem;" onchange="markDirty()" oninput="markDirty()">{_esc(row.get('analysis', ''))}</textarea>
  </div>
</div>

<div>
  <div style="display:flex;align-items:center;gap:0.3rem;">
    <div class="analysis-label">Steps to Reproduce</div>
    <button class="pencil-btn" onclick="editField('repro')"><svg viewBox="0 0 16 16"><path d="M12.1 1.3a1 1 0 011.4 0l1.2 1.2a1 1 0 010 1.4L5.8 12.8l-3.3.8.8-3.3L12.1 1.3zM2 14h12v1H2v-1z"/></svg></button>
  </div>
  <span class="field-view" id="view-repro">
    <div class="analysis-box{' empty' if not row.get('repro_steps') else ''}">{_format_analysis(row.get('repro_steps', '')) if row.get('repro_steps') else 'Not yet documented.'}</div>
  </span>
  <div class="inline-edit" id="edit-repro">
    <textarea id="ed-repro" rows="10" style="width:100%;font-family:'IBM Plex Mono',monospace;font-size:0.82rem;" onchange="markDirty()" oninput="markDirty()">{_esc(row.get('repro_steps', ''))}</textarea>
  </div>
</div>

<div>
  <div style="display:flex;align-items:center;gap:0.3rem;">
    <div class="analysis-label">Test Coverage</div>
    <button class="pencil-btn" onclick="editField('coverage')"><svg viewBox="0 0 16 16"><path d="M12.1 1.3a1 1 0 011.4 0l1.2 1.2a1 1 0 010 1.4L5.8 12.8l-3.3.8.8-3.3L12.1 1.3zM2 14h12v1H2v-1z"/></svg></button>
  </div>
  <span class="field-view" id="view-coverage">
    <div class="analysis-box{' empty' if not row.get('test_coverage') else ''}">{_format_analysis(row.get('test_coverage', '')) if row.get('test_coverage') else 'Not yet documented.'}</div>
  </span>
  <div class="inline-edit" id="edit-coverage">
    <textarea id="ed-coverage" rows="8" style="width:100%;font-family:'IBM Plex Mono',monospace;font-size:0.82rem;" onchange="markDirty()" oninput="markDirty()">{_esc(row.get('test_coverage', ''))}</textarea>
  </div>
</div>

<button class="settings-gear" id="settings-gear" onclick="toggleSettings()" title="Sync settings">
  <svg viewBox="0 0 20 20"><path d="M17.2 8.6l-1.5-.3a5.9 5.9 0 00-.5-1.2l.8-1.3a.5.5 0 00-.1-.6l-1.1-1.1a.5.5 0 00-.6-.1l-1.3.8a5.9 5.9 0 00-1.2-.5l-.3-1.5a.5.5 0 00-.5-.4H9.3a.5.5 0 00-.5.4l-.3 1.5a5.9 5.9 0 00-1.2.5l-1.3-.8a.5.5 0 00-.6.1L4.3 5.2a.5.5 0 00-.1.6l.8 1.3a5.9 5.9 0 00-.5 1.2l-1.5.3a.5.5 0 00-.4.5v1.6a.5.5 0 00.4.5l1.5.3c.1.4.3.8.5 1.2l-.8 1.3a.5.5 0 00.1.6l1.1 1.1a.5.5 0 00.6.1l1.3-.8c.4.2.8.4 1.2.5l.3 1.5a.5.5 0 00.5.4h1.6a.5.5 0 00.5-.4l.3-1.5c.4-.1.8-.3 1.2-.5l1.3.8a.5.5 0 00.6-.1l1.1-1.1a.5.5 0 00.1-.6l-.8-1.3c.2-.4.4-.8.5-1.2l1.5-.3a.5.5 0 00.4-.5V9.1a.5.5 0 00-.4-.5zM10 13a3 3 0 110-6 3 3 0 010 6z"/></svg>
</button>

<div class="settings-panel" id="settings-panel">
  <h4>GitHub Sync Settings</h4>
  <p>Save a Personal Access Token (repo scope) to sync edits to the shared GitHub repo. Without this, edits are saved locally in your browser only.</p>
  <div id="token-status" style="margin-bottom:0.5rem;font-size:0.78rem;"></div>
  <input type="password" id="gh-token-input" placeholder="ghp_xxxxxxxxxxxx">
  <div class="btn-row">
    <button class="btn-sm primary" onclick="saveToken()">Save Token</button>
    <button class="btn-sm danger" id="clear-token-btn" onclick="clearToken()" style="display:none;">Remove</button>
    <button class="btn-sm secondary" onclick="toggleSettings()">Close</button>
  </div>
</div>

<div class="save-bar" id="save-bar">
  <button class="save-btn" onclick="saveAll()">Save</button>
  <button class="discard-btn" onclick="discardAll()">Discard</button>
  <span class="change-count" id="change-count"></span>
  <span id="save-status" style="font-size:0.8rem;color:var(--ibm-gray-50);margin-left:auto;"></span>
</div>

{errors_html}

<h2>Timeline ({len(timeline)} events)</h2>
<div class="timeline">
{timeline_html}
</div>

<script>
const ISSUE_ID = '{_esc(row["jira_key"])}';
const LS_KEY = 'priohub_' + ISSUE_ID;
const GH_TOKEN_KEY = 'priohub_gh_token';
const REPO_OWNER = 'pdhiran';
const REPO_NAME = 'ceph-prio-hub';

const dirtyFields = new Set();

function getFieldData() {{
  return {{
    qa_status: document.getElementById('ed-qa-status').value,
    qa_assignee: document.getElementById('ed-qa-assignee').value,
    internal_priority: document.getElementById('ed-priority').value,
    hotfix_status: document.getElementById('ed-hotfix').value,
    notes: document.getElementById('ed-notes').value,
    analysis: document.getElementById('ed-analysis').value,
    repro_steps: document.getElementById('ed-repro').value,
    test_coverage: document.getElementById('ed-coverage').value,
  }};
}}

// Load locally saved overrides on page load
(function loadLocal() {{
  try {{
    const saved = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
    if (!saved) return;
    const fieldMap = {{
      qa_status: 'ed-qa-status', qa_assignee: 'ed-qa-assignee',
      internal_priority: 'ed-priority', hotfix_status: 'ed-hotfix',
      notes: 'ed-notes', analysis: 'ed-analysis',
      repro_steps: 'ed-repro', test_coverage: 'ed-coverage',
    }};
    Object.entries(fieldMap).forEach(([key, elId]) => {{
      if (saved[key] !== undefined) {{
        const el = document.getElementById(elId);
        if (el) el.value = saved[key];
      }}
    }});
  }} catch(e) {{}}
  refreshGearState();
}})();

function editField(field) {{
  const viewEl = document.getElementById('view-' + field);
  const editEl = document.getElementById('edit-' + field);
  if (!editEl) return;
  if (editEl.classList.contains('active')) {{
    editEl.classList.remove('active');
    if (viewEl) viewEl.classList.remove('hidden');
  }} else {{
    editEl.classList.add('active');
    if (viewEl) viewEl.classList.add('hidden');
    const input = editEl.querySelector('select, textarea');
    if (input) input.focus();
  }}
}}

function markDirty() {{
  const fields = ['qa_status','qa_assignee','priority','hotfix','notes','analysis','repro','coverage'];
  dirtyFields.clear();
  fields.forEach(f => {{
    const el = document.getElementById('edit-' + f);
    if (el && el.classList.contains('active')) dirtyFields.add(f);
  }});
  const bar = document.getElementById('save-bar');
  const count = document.getElementById('change-count');
  if (dirtyFields.size > 0) {{
    bar.classList.add('visible');
    count.textContent = dirtyFields.size + ' field' + (dirtyFields.size > 1 ? 's' : '') + ' modified';
  }} else {{
    bar.classList.remove('visible');
  }}
}}

function discardAll() {{
  document.querySelectorAll('.inline-edit.active').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.field-view.hidden').forEach(el => el.classList.remove('hidden'));
  dirtyFields.clear();
  document.getElementById('save-bar').classList.remove('visible');
}}

// --- Settings panel ---
function toggleSettings() {{
  const panel = document.getElementById('settings-panel');
  panel.classList.toggle('open');
  if (panel.classList.contains('open')) refreshTokenStatus();
}}

function refreshGearState() {{
  const gear = document.getElementById('settings-gear');
  gear.classList.toggle('configured', !!localStorage.getItem(GH_TOKEN_KEY));
}}

function refreshTokenStatus() {{
  const el = document.getElementById('token-status');
  const clearBtn = document.getElementById('clear-token-btn');
  if (localStorage.getItem(GH_TOKEN_KEY)) {{
    el.innerHTML = '<span class="sync-indicator on"></span> GitHub sync enabled';
    el.style.color = 'var(--ibm-green)';
    clearBtn.style.display = 'inline-block';
  }} else {{
    el.innerHTML = '<span class="sync-indicator off"></span> Local only (no sync)';
    el.style.color = 'var(--ibm-gray-50)';
    clearBtn.style.display = 'none';
  }}
}}

function saveToken() {{
  const input = document.getElementById('gh-token-input');
  const val = input.value.trim();
  if (val) {{
    localStorage.setItem(GH_TOKEN_KEY, val);
    input.value = '';
    refreshTokenStatus();
    refreshGearState();
  }}
}}

function clearToken() {{
  localStorage.removeItem(GH_TOKEN_KEY);
  refreshTokenStatus();
  refreshGearState();
}}

// --- Save: always localStorage, optionally GitHub ---
async function saveAll() {{
  const data = getFieldData();
  const status = document.getElementById('save-status');

  // 1. Always save to localStorage
  localStorage.setItem(LS_KEY, JSON.stringify(data));

  // 2. If GitHub token configured, also push to repo
  const token = localStorage.getItem(GH_TOKEN_KEY);
  if (token) {{
    status.textContent = 'Syncing to GitHub...';
    status.style.color = 'var(--ibm-blue)';
    try {{
      const url = `https://api.github.com/repos/${{REPO_OWNER}}/${{REPO_NAME}}/contents/tracking.json`;
      const resp = await fetch(url, {{ headers: {{ Authorization: `token ${{token}}` }} }});
      let trackingData = {{ issues: {{}} }};
      let sha = null;
      if (resp.ok) {{
        const file = await resp.json();
        sha = file.sha;
        trackingData = JSON.parse(atob(file.content));
      }}
      if (!trackingData.issues) trackingData.issues = {{}};
      if (!trackingData.issues[ISSUE_ID]) trackingData.issues[ISSUE_ID] = {{}};
      Object.assign(trackingData.issues[ISSUE_ID], data);

      const body = {{
        message: `Update tracking: ${{ISSUE_ID}}`,
        content: btoa(unescape(encodeURIComponent(JSON.stringify(trackingData, null, 2)))),
      }};
      if (sha) body.sha = sha;

      const putResp = await fetch(url, {{
        method: 'PUT',
        headers: {{ Authorization: `token ${{token}}`, 'Content-Type': 'application/json' }},
        body: JSON.stringify(body),
      }});
      if (putResp.ok) {{
        status.textContent = 'Saved + synced to GitHub';
        status.style.color = 'var(--ibm-green)';
      }} else {{
        if (putResp.status === 401) {{
          localStorage.removeItem(GH_TOKEN_KEY);
          refreshGearState();
          status.textContent = 'Saved locally. Token expired -- update in settings.';
        }} else {{
          status.textContent = 'Saved locally. GitHub sync failed (' + putResp.status + ')';
        }}
        status.style.color = 'var(--ibm-orange)';
      }}
    }} catch (e) {{
      status.textContent = 'Saved locally. GitHub sync error.';
      status.style.color = 'var(--ibm-orange)';
    }}
  }} else {{
    status.textContent = 'Saved locally';
    status.style.color = 'var(--ibm-green)';
  }}
  setTimeout(() => {{ status.textContent = ''; }}, 4000);
}}
</script>
</body>
</html>"""
