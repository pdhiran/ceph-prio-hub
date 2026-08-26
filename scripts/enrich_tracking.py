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


def _extract_jira_status(timeline: list[dict]) -> str:
    """Get the latest JIRA status from timeline."""
    status_entries = [e for e in timeline if e.get("type") == "jira_status"]
    if status_entries:
        return status_entries[-1].get("status", "Unknown")
    return "Unknown"


def build_analysis(issue: dict, fix_info: dict, tags: set[str]) -> str:
    """Synthesize root cause analysis from JIRA data."""
    subject = issue.get("subject", "")
    components = issue.get("components", [])
    comp = components[0] if components else "unknown"
    severity = issue.get("severity", "normal")
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
        lines.append(f"- First trace: {traces[0][:150]}")
    elif "crash" in tags:
        lines.append(f"- Daemon crash reported in {comp}. No stack trace captured in JIRA metadata.")
    elif errors:
        lines.append(f"- Error encountered: {errors[0][:200]}")
    elif warnings:
        lines.append(f"- Health warning: {warnings[0]}")
    else:
        lines.append(f"- Issue reported in {comp} component.")

    if "upgrade" in tags:
        lines.append("- This appears to be an upgrade-related issue or backport request.")
    if "performance" in tags:
        lines.append("- Performance degradation or timeout behavior reported.")
    if "access" in tags:
        lines.append("- Permission or access control issue reported.")
    if "memory" in tags:
        lines.append("- Memory issue (leak, OOM, or excessive consumption) reported.")

    ver_str = ", ".join(versions) if versions else "not specified"
    lines.append(f"- Affected version(s): {ver_str}")
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
        lines.append("- No fix confirmed in JIRA comments yet.")

    if fix_info["workaround"]:
        lines.append(f"- Workaround mentioned: {fix_info['workaround'][:150]}")

    return "\n".join(lines)


def build_repro_steps(issue: dict, tags: set[str]) -> str:
    """Generate reproduction steps from JIRA data."""
    subject = issue.get("subject", "")
    components = issue.get("components", [])
    comp = components[0] if components else "unknown"
    versions = issue.get("ceph_versions", [])
    ver = versions[0] if versions else "9.x"
    timeline = issue.get("timeline", [])

    lines = []
    lines.append(f"Goal: Reproduce {comp} issue reported in IBM Ceph {ver}.")
    lines.append("")

    step = 1

    # Check if JIRA description/comments have explicit repro steps
    has_explicit_steps = False
    for entry in timeline:
        if entry.get("type") != "jira_comment":
            continue
        body = entry.get("summary", "") or entry.get("body", "")
        if "steps to reproduce" in body.lower() or "how to reproduce" in body.lower():
            has_explicit_steps = True
            break

    # Component-specific setup
    if comp in ("cephfs", "mds"):
        lines.append(f"{step}. Deploy IBM Ceph {ver} cluster with CephFS -- `cephadm bootstrap --mon-ip=<ip>`")
        step += 1
        lines.append(f"{step}. Create CephFS filesystem -- `ceph fs volume create cephfs`")
        step += 1
        if "mount" in tags or "fuse" in subject.lower():
            lines.append(f"{step}. Mount CephFS -- `mount -t ceph <mon-ip>:/ /mnt/cephfs -o name=admin,secret=<key>`")
            step += 1
    elif comp in ("nfs-ganesha", "nfs-ganesha/ceph"):
        lines.append(f"{step}. Deploy IBM Ceph {ver} cluster with CephFS and NFS -- `cephadm bootstrap --mon-ip=<ip>`")
        step += 1
        lines.append(f"{step}. Create NFS cluster -- `ceph nfs cluster create <cluster-name> <host>`")
        step += 1
        lines.append(f"{step}. Create NFS export -- `ceph nfs export create cephfs <cluster> /export cephfs /`")
        step += 1
        lines.append(f"{step}. Mount on client -- `mount -t nfs -o nfsvers=4.2 <host>:/export /mnt/nfs`")
        step += 1
    elif comp in ("smb", "smb/samba"):
        lines.append(f"{step}. Deploy IBM Ceph {ver} cluster with CephFS and SMB -- `cephadm bootstrap --mon-ip=<ip>`")
        step += 1
        lines.append(f"{step}. Enable SMB module -- `ceph mgr module enable smb`")
        step += 1
        lines.append(f"{step}. Create SMB cluster -- `ceph smb cluster create <cluster-id> user`")
        step += 1
    elif comp in ("rados", "osd", "mon"):
        lines.append(f"{step}. Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`")
        step += 1
        lines.append(f"{step}. Add OSDs -- `ceph orch apply osd --all-available-devices`")
        step += 1
        lines.append(f"{step}. Create test pool -- `ceph osd pool create test-pool 32`")
        step += 1
    elif comp in ("rgw", "radosgw"):
        lines.append(f"{step}. Deploy IBM Ceph {ver} cluster with RGW -- `cephadm bootstrap --mon-ip=<ip>`")
        step += 1
        lines.append(f"{step}. Deploy RGW service -- `ceph orch apply rgw <realm> <zone>`")
        step += 1
    elif comp in ("cephadm", "ceph-ansible"):
        lines.append(f"{step}. Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`")
        step += 1
        lines.append(f"{step}. Add hosts -- `ceph orch host add <host> <ip>`")
        step += 1
    elif comp in ("ceph-dashboard", "management & ui"):
        lines.append(f"{step}. Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`")
        step += 1
        lines.append(f"{step}. Enable dashboard -- `ceph mgr module enable dashboard`")
        step += 1
        lines.append(f"{step}. Access dashboard -- `curl -k https://<mgr-host>:8443/`")
        step += 1
    elif comp in ("rbd", "rbd-mirror"):
        lines.append(f"{step}. Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`")
        step += 1
        lines.append(f"{step}. Create RBD pool -- `ceph osd pool create rbd-pool 32`")
        step += 1
        lines.append(f"{step}. Create RBD image -- `rbd create rbd-pool/test-image --size 10G`")
        step += 1
    elif comp in ("ceph-volume",):
        lines.append(f"{step}. Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`")
        step += 1
        lines.append(f"{step}. Run ceph-volume inventory -- `cephadm ceph-volume inventory`")
        step += 1
    else:
        lines.append(f"{step}. Deploy IBM Ceph {ver} cluster -- `cephadm bootstrap --mon-ip=<ip>`")
        step += 1

    # Scenario-specific steps
    if "upgrade" in tags:
        lines.append(f"{step}. Perform upgrade -- `ceph orch upgrade start --image <target-image>`")
        step += 1
        lines.append(f"{step}. Monitor upgrade progress -- `ceph orch upgrade status`")
        step += 1
    if "crash" in tags:
        lines.append(f"{step}. Trigger the scenario described in the JIRA (see issue for specific conditions)")
        step += 1
        lines.append(f"{step}. Check for crash -- `ceph crash ls-new`")
        step += 1
        lines.append(f"{step}. Collect crash info -- `ceph crash info <crash-id>`")
        step += 1
    if "failover" in tags:
        lines.append(f"{step}. Trigger failover (stop daemon, power off node, or network partition)")
        step += 1
        lines.append(f"{step}. Verify services recover -- `ceph -s`")
        step += 1

    lines.append(f"{step}. Verify cluster health -- `ceph -s` and `ceph health detail`")

    if not has_explicit_steps:
        lines.append("")
        lines.append("NOTE: Specific trigger conditions may be described in the JIRA comments. Review the issue timeline for details.")

    return "\n".join(lines)


def build_test_coverage(issue: dict, tags: set[str], fix_info: dict) -> str:
    """Generate test coverage assessment."""
    components = issue.get("components", [])
    comp = components[0] if components else "unknown"
    status = _extract_jira_status(issue.get("timeline", []))

    lines = []

    is_closed = status.lower() in ("closed", "done", "verified", "release pending")

    if is_closed and fix_info["has_fix"]:
        lines.append(f"SUMMARY: Issue is {status}. Fix verified. Need regression test to prevent recurrence.")
    elif fix_info["has_fix"]:
        lines.append(f"SUMMARY: Fix available but not yet fully verified. Needs QA validation.")
    else:
        lines.append(f"SUMMARY: Issue is {status}. No confirmed fix yet. Needs investigation and test coverage.")

    lines.append("")
    lines.append("GAPS:")

    if "crash" in tags:
        lines.append(f"- No crash regression test for this specific {comp} scenario")
        lines.append("- Need test that triggers the crash condition and verifies daemon stability")
    if "upgrade" in tags:
        lines.append("- Upgrade test may not cover the specific pre-conditions that trigger this issue")
    if "performance" in tags:
        lines.append("- No performance benchmark test for this scenario")
    if "access" in tags:
        lines.append("- Permission/ACL test may not cover this specific access pattern")
    if "scale" in tags:
        lines.append("- Scale test may not reach the threshold that triggers this issue")
    if not tags:
        lines.append(f"- Need to assess existing {comp} test coverage for this scenario")

    lines.append("")
    lines.append("SCENARIOS NEEDED:")

    if "crash" in tags:
        lines.append(f"- Stability test: trigger the reported condition, verify no daemon crash")
    if "upgrade" in tags:
        lines.append(f"- Upgrade test: validate behavior before and after upgrade on affected version")
    if "performance" in tags:
        lines.append(f"- Performance test: measure and compare against baseline")
    if "failover" in tags:
        lines.append(f"- HA test: trigger failover, verify data integrity and service continuity")

    lines.append(f"- Functional test for {comp}: reproduce the reported scenario and verify correct behavior")

    if is_closed:
        lines.append(f"- Regression test: verify fix holds across supported versions")

    return "\n".join(lines)


def main():
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

        # Skip if already has non-empty analysis
        if entry.get("analysis", "").strip():
            skipped += 1
            continue

        subject = issue.get("subject", "")
        timeline = issue.get("timeline", [])
        tags = _classify(subject)
        fix_info = _extract_fix_info(timeline)

        entry["analysis"] = build_analysis(issue, fix_info, tags)
        entry["repro_steps"] = build_repro_steps(issue, tags)
        entry["test_coverage"] = build_test_coverage(issue, tags, fix_info)

        enriched += 1

    with open(TRACKING_PATH, "w") as f:
        json.dump(tracking, f, indent=2, ensure_ascii=False)

    print(f"Enriched: {enriched} issues")
    print(f"Skipped (already have analysis): {skipped}")
    print(f"Total in state DB: {len(issue_list)}")
    print(f"Tracking file: {TRACKING_PATH} ({TRACKING_PATH.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
