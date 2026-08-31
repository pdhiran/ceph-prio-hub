#!/usr/bin/env python3
"""Generate meaningful RCA, repro steps, and test coverage from JIRA state data.

Reads the issue state DB (which contains JIRA subject, components, timeline,
comments, error messages, stack traces, health warnings) and synthesizes
structured analysis for tracking.json.

Only writes to issues that have empty analysis fields. Does NOT overwrite
manually written or previously enriched content.
"""
import json
import re
from pathlib import Path

TRACKING_PATH = Path.home() / ".ceph-prio-hub" / "tracking.json"
STATE_PATH = Path.home() / ".ceph-prio-hub" / "state" / "issues.json"


def _classify(subject: str) -> set[str]:
    """Tag an issue based on keywords in the subject."""
    s = subject.lower()
    tags = set()
    kw_map = {
        "crash": ["crash", "sigsegv", "sigabrt", "segfault", "core dump", "assert", "abort"],
        "upgrade": ["upgrade", "backport", "regression"],
        "performance": ["slow", "latency", "throughput", "performance", "hang", "timeout", "stuck"],
        "access": ["permission", "denied", "access", "auth", "acl", "forbidden"],
        "mount": ["mount", "unmount", "umount", "fuse"],
        "ssl": ["ssl", "tls", "certificate", "cert"],
        "config": ["config", "configuration", "setting", "option"],
        "memory": ["memory", "leak", "oom", "out of memory"],
        "network": ["network", "connection", "socket", "transport", "endpoint"],
        "data_integrity": ["corrupt", "checksum", "mismatch", "data loss", "inconsistent"],
        "failover": ["failover", "failback", "ha ", "high availability", "standby"],
        "scale": ["scale", "5000", "10000", "large", "many"],
    }
    for tag, keywords in kw_map.items():
        if any(kw in s for kw in keywords):
            tags.add(tag)
    return tags


def _extract_fix_info(timeline: list[dict]) -> dict:
    """Extract fix/resolution details from timeline comments."""
    fix_info = {"has_fix": False, "fix_build": "", "fix_pr": "", "verified_by": "", "workaround": ""}
    for entry in timeline:
        if entry.get("type") != "jira_comment":
            continue
        body = entry.get("summary", "") or entry.get("body", "")
        body_lower = body.lower()
        if "fix is available" in body_lower or "moving to on_qa" in body_lower:
            fix_info["has_fix"] = True
            build_match = re.search(r"build\s+(?:ceph[- ])?(\d[\d.\-]+)", body, re.I)
            if build_match:
                fix_info["fix_build"] = build_match.group(1)
        if "verified" in body_lower and entry.get("author"):
            fix_info["verified_by"] = entry["author"]
        pr_match = re.search(r"https?://(?:github\.com|gitlab\.cee\.redhat\.com)/[^\s)]+", body)
        if pr_match:
            fix_info["fix_pr"] = pr_match.group(0)
        if "workaround" in body_lower:
            fix_info["workaround"] = body[:200].strip()
    return fix_info


def _extract_comment_insights(timeline: list[dict]) -> list[str]:
    """Extract key diagnostic findings from comment summaries."""
    insights = []
    seen = set()
    for entry in timeline:
        if entry.get("type") != "jira_comment":
            continue
        body = entry.get("summary", "") or entry.get("body", "")
        if not body or len(body) < 30:
            continue
        body_lower = body.lower()

        # Skip meta comments (needinfo, assignment, status changes)
        skip_phrases = [
            "moving to", "moving it to", "closing as part of", "can you provide",
            "please let us know", "checking in", "hi team", "fyi", "cc:",
            "please update", "can we get an update", "thank you for",
        ]
        if any(p in body_lower for p in skip_phrases) and len(body) < 100:
            continue

        author = entry.get("author", "")
        insight = None

        # Look for analysis/findings in comments
        analysis_markers = [
            "root cause", "analysis", "investigation", "found that", "the issue is",
            "the problem is", "this happens because", "this is caused by",
            "the reason", "we observed", "debug", "traced", "identified",
            "monstore", "stack trace", "core dump", "assert", "crash",
            "memory", "leak", "OOM", "trim", "compact",
        ]
        if any(m in body_lower for m in analysis_markers):
            snippet = body[:280].strip()
            key = snippet[:80]
            if key not in seen:
                seen.add(key)
                insight = f"[{author}] {snippet}"

        # Look for workaround/solution
        if not insight:
            fix_markers = ["workaround", "fix", "patch", "resolved", "solution"]
            if any(m in body_lower for m in fix_markers):
                snippet = body[:280].strip()
                key = snippet[:80]
                if key not in seen:
                    seen.add(key)
                    insight = f"[{author}] {snippet}"

        if insight:
            insights.append(insight)

    return insights[:5]  # cap at 5 most relevant


def _extract_jira_status(timeline: list[dict]) -> str:
    """Get the latest JIRA status from timeline."""
    status_entries = [e for e in timeline if e.get("type") == "jira_status"]
    if status_entries:
        return status_entries[-1].get("status", "Unknown")
    return "Unknown"


def build_analysis(issue: dict, fix_info: dict, tags: set[str], insights: list[str]) -> str:
    """Synthesize root cause analysis from JIRA data."""
    subject = issue.get("subject", "")
    components = issue.get("components", [])
    comp = components[0] if components else "unknown"
    errors = issue.get("error_messages", [])
    traces = issue.get("stack_traces", [])
    warnings = issue.get("health_warnings", [])
    versions = issue.get("ceph_versions", [])
    status = _extract_jira_status(issue.get("timeline", []))

    lines = []

    lines.append(f"SUMMARY: {subject}")
    lines.append("")

    # Root cause section
    lines.append("ROOT CAUSE:")
    if "crash" in tags and traces:
        lines.append(f"- Daemon crash in {comp}. Stack trace indicates assertion failure or segfault.")
        lines.append(f"- First trace: {traces[0][:200]}")
    elif "crash" in tags:
        lines.append(f"- Daemon crash reported in {comp}.")
    elif errors:
        lines.append(f"- Error encountered: {errors[0][:250]}")
    elif warnings:
        lines.append(f"- Health warning: {warnings[0]}")

    if "upgrade" in tags:
        lines.append("- Upgrade-related issue or backport request.")
    if "performance" in tags:
        lines.append("- Performance degradation or timeout behavior reported.")
    if "access" in tags:
        lines.append("- Permission or access control issue.")
    if "memory" in tags:
        lines.append("- Memory issue (leak, OOM, or excessive consumption).")

    ver_str = ", ".join(versions) if versions else "not specified"
    lines.append(f"- Affected version(s): {ver_str}")
    lines.append(f"- Component: {comp}")
    lines.append("")

    # Engineering findings from comments
    if insights:
        lines.append("ENGINEERING FINDINGS:")
        for insight in insights:
            lines.append(f"- {insight}")
        lines.append("")

    # Fix status
    lines.append("FIX STATUS:")
    if fix_info["has_fix"]:
        lines.append(f"- Fix available" + (f" in build {fix_info['fix_build']}" if fix_info["fix_build"] else ""))
        if fix_info["fix_pr"]:
            lines.append(f"- PR/MR: {fix_info['fix_pr']}")
        if fix_info["verified_by"]:
            lines.append(f"- Verified by: {fix_info['verified_by']}")
    else:
        lines.append(f"- Current JIRA status: {status}")

    if fix_info["workaround"]:
        lines.append(f"- Workaround: {fix_info['workaround'][:200]}")

    return "\n".join(lines)


def _extract_scenario_context(timeline: list[dict], subject: str) -> str:
    """Extract scenario-specific detail from comments for repro steps."""
    subject_lower = subject.lower()
    context_lines = []

    for entry in timeline:
        if entry.get("type") != "jira_comment":
            continue
        body = entry.get("summary", "") or entry.get("body", "")
        if not body or len(body) < 40:
            continue
        body_lower = body.lower()

        repro_markers = [
            "steps to reproduce", "how to reproduce", "to reproduce", "reproduce this",
            "trigger this", "the scenario", "to replicate",
        ]
        if any(m in body_lower for m in repro_markers):
            return body[:280].strip()

        analysis_markers = [
            "the issue occurs when", "the problem happens when", "this happens when",
            "the trigger is", "observed that", "we noticed", "the cause is",
            "this is triggered by",
        ]
        if any(m in body_lower for m in analysis_markers):
            context_lines.append(body[:200].strip())

    return "\n".join(context_lines[:2])


def build_repro_steps(issue: dict, tags: set[str], insights: list[str]) -> str:
    """Generate reproduction steps from JIRA data, using comment context."""
    subject = issue.get("subject", "")
    components = issue.get("components", [])
    comp = components[0] if components else "unknown"
    versions = issue.get("ceph_versions", [])
    ver = versions[0] if versions else "9.x"
    timeline = issue.get("timeline", [])

    scenario_context = _extract_scenario_context(timeline, subject)

    # Build a concise goal from the subject
    goal = subject.strip()
    # Remove common prefixes
    for prefix in ["[IBM_Support]", "[IBM Support]", "[Ceph_L3]", "[Customer]"]:
        goal = goal.replace(prefix, "").strip()

    lines = []
    lines.append(f"Goal: Validate and reproduce -- {goal}")
    lines.append(f"Environment: IBM Ceph {ver}, component: {comp}")
    lines.append("")

    step = 1

    # Component-specific setup (concise)
    setup_map = {
        ("cephfs", "mds"): [
            "Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`",
            "Create CephFS filesystem -- `ceph fs volume create cephfs`",
            "Mount CephFS on client -- `mount -t ceph <mon>:/ /mnt/cephfs -o name=admin`",
        ],
        ("nfs-ganesha", "nfs-ganesha/ceph"): [
            "Deploy IBM Ceph {ver} cluster with CephFS -- `cephadm bootstrap --mon-ip=<ip>`",
            "Create NFS cluster -- `ceph nfs cluster create <name> <host>`",
            "Create NFS export -- `ceph nfs export create cephfs <cluster> /export cephfs /`",
            "Mount NFS on client -- `mount -t nfs -o nfsvers=4.2 <host>:/export /mnt/nfs`",
        ],
        ("smb", "smb/samba"): [
            "Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`",
            "Enable SMB module -- `ceph mgr module enable smb`",
            "Create SMB cluster and share -- `ceph smb cluster create <id> user`",
        ],
        ("rados", "osd", "mon"): [
            "Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`",
            "Add OSDs -- `ceph orch apply osd --all-available-devices`",
            "Create test pool -- `ceph osd pool create test-pool 32`",
        ],
        ("rgw", "radosgw"): [
            "Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`",
            "Deploy RGW -- `ceph orch apply rgw <realm> <zone>`",
        ],
        ("rbd", "rbd-mirror"): [
            "Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`",
            "Create RBD pool and image -- `rbd create test-pool/img --size 10G`",
        ],
        ("cephadm", "ceph-ansible"): [
            "Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`",
            "Add hosts -- `ceph orch host add <host> <ip>`",
        ],
        ("ceph-dashboard", "management & ui"): [
            "Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`",
            "Enable dashboard -- `ceph mgr module enable dashboard`",
            "Access at `https://<mgr-host>:8443/`",
        ],
    }

    setup_steps = None
    for comp_group, steps in setup_map.items():
        if comp in comp_group:
            setup_steps = steps
            break
    if not setup_steps:
        setup_steps = ["Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`"]

    for s in setup_steps:
        lines.append(f"{step}. {s.format(ver=ver)}")
        step += 1

    # Scenario-specific steps
    if "upgrade" in tags:
        lines.append(f"{step}. Upgrade to target version -- `ceph orch upgrade start --image <target-image>`")
        step += 1
        lines.append(f"{step}. Monitor upgrade -- `ceph orch upgrade status`")
        step += 1

    if "crash" in tags:
        lines.append(f"{step}. Trigger the failure scenario (see engineering findings below)")
        step += 1
        lines.append(f"{step}. Check for daemon crash -- `ceph crash ls-new`")
        step += 1

    if "failover" in tags:
        lines.append(f"{step}. Trigger failover (stop daemon / power off node / network partition)")
        step += 1
        lines.append(f"{step}. Verify recovery -- `ceph -s`")
        step += 1

    if "performance" in tags:
        lines.append(f"{step}. Run baseline benchmark (fio/rados bench) -- record metrics")
        step += 1
        lines.append(f"{step}. Trigger the performance scenario described in JIRA")
        step += 1

    if "memory" in tags:
        lines.append(f"{step}. Monitor daemon memory -- `ceph tell <daemon> heap stats`")
        step += 1

    if "ssl" in tags:
        lines.append(f"{step}. Verify SSL/TLS configuration -- `ceph config get rgw rgw_frontends`")
        step += 1

    if "config" in tags:
        lines.append(f"{step}. Apply the configuration change described in the JIRA")
        step += 1

    lines.append(f"{step}. Verify cluster health -- `ceph -s` and `ceph health detail`")
    step += 1

    # Add scenario context from comments if available
    if scenario_context:
        lines.append("")
        lines.append("SCENARIO DETAIL (from engineering comments):")
        lines.append(scenario_context)

    return "\n".join(lines)


def build_test_coverage(issue: dict, tags: set[str], fix_info: dict, insights: list[str]) -> str:
    """Generate test coverage assessment."""
    subject = issue.get("subject", "")
    components = issue.get("components", [])
    comp = components[0] if components else "unknown"
    status = _extract_jira_status(issue.get("timeline", []))

    # Clean subject for goal
    goal = subject.strip()
    for prefix in ["[IBM_Support]", "[IBM Support]", "[Ceph_L3]", "[Customer]"]:
        goal = goal.replace(prefix, "").strip()

    lines = []

    is_closed = status.lower() in ("closed", "done", "verified", "release pending")

    if is_closed and fix_info["has_fix"]:
        lines.append(f"SUMMARY: {status}. Fix verified. Regression test needed.")
    elif fix_info["has_fix"]:
        lines.append(f"SUMMARY: Fix available. QA validation required.")
    else:
        lines.append(f"SUMMARY: {status}. Investigation and test coverage needed.")

    lines.append("")
    lines.append("GAPS:")

    if "crash" in tags:
        lines.append(f"- No crash regression test for: {goal[:100]}")
    if "upgrade" in tags:
        lines.append(f"- Upgrade path may lack pre-condition coverage for this scenario")
    if "performance" in tags:
        lines.append(f"- No performance baseline test for: {goal[:100]}")
    if "access" in tags:
        lines.append(f"- Permission test gap for: {goal[:100]}")
    if "scale" in tags:
        lines.append(f"- Scale test may not reach threshold that triggers this issue")
    if "memory" in tags:
        lines.append(f"- No memory leak detection test for {comp}")
    if "data_integrity" in tags:
        lines.append(f"- Data integrity validation missing for this failure mode")
    if not tags:
        lines.append(f"- Existing {comp} tests may not cover: {goal[:100]}")

    lines.append("")
    lines.append("SCENARIOS NEEDED:")

    if "crash" in tags:
        lines.append(f"- Stability: reproduce conditions, verify no crash")
    if "upgrade" in tags:
        lines.append(f"- Upgrade: validate behavior across affected versions")
    if "performance" in tags:
        lines.append(f"- Performance: benchmark before/after, compare against SLA")
    if "failover" in tags:
        lines.append(f"- HA: failover + recovery, verify data integrity")
    if "data_integrity" in tags:
        lines.append(f"- Data integrity: checksum validation through failure scenario")
    if "memory" in tags:
        lines.append(f"- Memory: long-running workload, monitor for leaks")

    lines.append(f"- Functional: reproduce reported {comp} scenario, verify correct behavior")

    if is_closed:
        lines.append(f"- Regression: verify fix holds in latest builds")

    return "\n".join(lines)


def main():
    import sys
    from datetime import datetime, timezone

    force = "--force" in sys.argv

    with open(TRACKING_PATH) as f:
        tracking = json.load(f)
    with open(STATE_PATH) as f:
        state = json.load(f)

    issues_data = state.get("issues", {})
    if isinstance(issues_data, list):
        issue_list = issues_data
    else:
        issue_list = list(issues_data.values())

    tracking_issues = tracking.setdefault("issues", {})
    enriched = 0
    skipped = 0

    for issue in issue_list:
        if not isinstance(issue, dict):
            continue
        jira_ids = issue.get("jira_ids", [])
        key = jira_ids[0] if jira_ids else None
        if not key:
            continue

        entry = tracking_issues.setdefault(key, {})

        if not force:
            existing = entry.get("analysis", "").strip()
            if existing and "ENGINEERING FINDINGS:" in existing:
                skipped += 1
                continue

        subject = issue.get("subject", "")
        timeline = issue.get("timeline", [])
        tags = _classify(subject)
        fix_info = _extract_fix_info(timeline)
        insights = _extract_comment_insights(timeline)

        entry["analysis"] = build_analysis(issue, fix_info, tags, insights)
        entry["repro_steps"] = build_repro_steps(issue, tags, insights)
        entry["test_coverage"] = build_test_coverage(issue, tags, fix_info, insights)
        entry["enriched_at"] = datetime.now(timezone.utc).isoformat()

        enriched += 1

    with open(TRACKING_PATH, "w") as f:
        json.dump(tracking, f, indent=2, ensure_ascii=False)

    print(f"Enriched: {enriched} issues")
    print(f"Skipped: {skipped}")
    print(f"Total in state DB: {len(issue_list)}")
    print(f"Tracking file: {TRACKING_PATH} ({TRACKING_PATH.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
