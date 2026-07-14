"""Extract structured issue information from prio-list email text.

Parses case IDs, JIRA IDs, Ceph versions, component names, error messages,
stack traces, and severity indicators from email subjects and bodies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ExtractedIssueInfo:
    """Structured data extracted from an email body/subject."""

    case_ids: list[str] = field(default_factory=list)
    jira_ids: list[str] = field(default_factory=list)
    bugzilla_ids: list[str] = field(default_factory=list)
    tracker_ids: list[str] = field(default_factory=list)
    ceph_versions: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    error_messages: list[str] = field(default_factory=list)
    stack_traces: list[str] = field(default_factory=list)
    health_warnings: list[str] = field(default_factory=list)
    severity: str | None = None
    jira_labels: list[str] = field(default_factory=list)

    @property
    def all_tracker_ids(self) -> list[str]:
        return self.case_ids + self.jira_ids + self.bugzilla_ids + self.tracker_ids

    def to_dict(self) -> dict:
        return {
            "case_ids": self.case_ids,
            "jira_ids": self.jira_ids,
            "bugzilla_ids": self.bugzilla_ids,
            "tracker_ids": self.tracker_ids,
            "ceph_versions": self.ceph_versions,
            "components": self.components,
            "error_messages": self.error_messages,
            "stack_traces": self.stack_traces,
            "health_warnings": self.health_warnings,
            "severity": self.severity,
            "jira_labels": self.jira_labels,
        }


# -- Case/ticket ID patterns --

# Support case numbers in subject: [04486725] or (04486725)
_RE_CASE_ID_BRACKET = re.compile(r"\[(\d{7,8})\]")
_RE_CASE_ID_PAREN = re.compile(r"\((\d{7,8})\)")
# Case number in prose: "case 04486725" / "Case#04486725" / "ticket 04486725"
_RE_CASE_ID_PROSE = re.compile(
    r"(?:case|ticket|support\s*case)\s*[#: ]?\s*(\d{7,8})\b", re.IGNORECASE
)

# JIRA IDs: IBMCEPH-12345, RHCEPH-1234, CEPH-5678
_RE_JIRA_ID = re.compile(r"\b((?:IBMCEPH|RHCEPH|CEPH)-\d{3,6})\b")

# Bugzilla: BZ#1234567 or bz 1234567
_RE_BZ_ID = re.compile(r"\bBZ\s*#?\s*(\d{6,7})\b", re.IGNORECASE)

# Ceph Tracker: tracker #12345
_RE_TRACKER_ID = re.compile(
    r"(?:ceph\s+)?tracker\s*#?\s*(\d{4,6})\b", re.IGNORECASE
)

# -- Ceph version patterns --

# "Ceph 18.2.1" / "ceph version 18.2.1-123" / "RH Ceph 5.2.6" / "IBM Ceph 8.1"
_RE_CEPH_VERSION = re.compile(
    r"(?:RH\s+|IBM\s+|Red\s+Hat\s+)?Ceph\s+(?:Storage\s+)?(?:version\s+)?(\d+\.\d+(?:\.\d+)?(?:-\d+[a-zA-Z0-9.]*)?)",
    re.IGNORECASE,
)
# ODF version: "ODF 4.12.14"
_RE_ODF_VERSION = re.compile(
    r"ODF\s+(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE
)

# -- Component patterns --

CEPH_COMPONENTS = [
    "RGW", "RBD", "CephFS", "NFS", "MDS", "OSD", "MON", "MGR",
    "RADOS", "RBD-Mirror", "iSCSI", "NVMeoF", "Dashboard",
    "Cephadm", "Orchestrator", "ceph-volume", "BlueStore",
    "Crimson", "RGW-Multisite", "S3", "Swift",
]
_COMPONENT_PATTERNS = {
    comp: re.compile(rf"\b{re.escape(comp)}\b", re.IGNORECASE)
    for comp in CEPH_COMPONENTS
}
_COMPONENT_ALIASES = {
    "object gateway": "RGW",
    "object store": "RGW",
    "block device": "RBD",
    "file system": "CephFS",
    "metadata server": "MDS",
    "object storage daemon": "OSD",
    "monitor": "MON",
    "manager": "MGR",
    "nfs-ganesha": "NFS",
    "ganesha": "NFS",
}
_RE_COMPONENT_ALIASES = {
    alias: comp
    for alias, comp in _COMPONENT_ALIASES.items()
}

# -- Error / stack trace patterns --

_RE_ERROR_LINE = re.compile(
    r"^.*(?:ERROR|FATAL|CRITICAL|Traceback|AssertionError|segfault|SIGABRT|core dump|oom-kill).*$",
    re.MULTILINE | re.IGNORECASE,
)

_RE_STACK_TRACE = re.compile(
    r"((?:Traceback \(most recent call last\):.*?(?:\n\S|\Z))"
    r"|(?:(?:#\d+\s+0x[0-9a-f]+\s+in\s+\S+.*\n?){3,}))",
    re.DOTALL,
)

_RE_HEALTH_WARNING = re.compile(
    r"\b(HEALTH_(?:WARN|ERR)\b[^\n]{0,120})",
    re.IGNORECASE,
)

# -- Severity patterns --

_RE_SEVERITY = re.compile(
    r"\b(?:severity|sev|priority)\s*[:=]?\s*(urgent|critical|high|medium|low|[1-4]|P[0-3])\b",
    re.IGNORECASE,
)

_SEVERITY_MAP = {
    "urgent": "critical",
    "1": "critical",
    "p0": "critical",
    "2": "high",
    "p1": "high",
    "3": "medium",
    "p2": "medium",
    "4": "low",
    "p3": "low",
}

# -- JIRA labels --

_RE_JIRA_LABEL = re.compile(
    r"\b(Ceph_L[1-3]|IBM_Customer_Issue|customer_escalation|regression|blocker)\b",
    re.IGNORECASE,
)


def extract_issue_info(subject: str, body: str) -> ExtractedIssueInfo:
    """Extract structured issue data from an email subject and body.

    Args:
        subject: Email subject line.
        body: Email body text (plain text, not HTML).

    Returns:
        ExtractedIssueInfo with all extracted fields.
    """
    text = f"{subject}\n{body}"
    info = ExtractedIssueInfo()

    # Case IDs
    for pattern in [_RE_CASE_ID_BRACKET, _RE_CASE_ID_PAREN, _RE_CASE_ID_PROSE]:
        for match in pattern.finditer(text):
            cid = match.group(1)
            if cid not in info.case_ids:
                info.case_ids.append(cid)

    # JIRA IDs
    for match in _RE_JIRA_ID.finditer(text):
        jid = match.group(1).upper()
        if jid not in info.jira_ids:
            info.jira_ids.append(jid)

    # Bugzilla IDs
    for match in _RE_BZ_ID.finditer(text):
        bid = match.group(1)
        if bid not in info.bugzilla_ids:
            info.bugzilla_ids.append(bid)

    # Ceph Tracker IDs
    for match in _RE_TRACKER_ID.finditer(text):
        tid = match.group(1)
        if tid not in info.tracker_ids:
            info.tracker_ids.append(tid)

    # Ceph versions
    for match in _RE_CEPH_VERSION.finditer(text):
        ver = match.group(1)
        if ver not in info.ceph_versions:
            info.ceph_versions.append(ver)

    for match in _RE_ODF_VERSION.finditer(text):
        ver = f"ODF {match.group(1)}"
        if ver not in info.ceph_versions:
            info.ceph_versions.append(ver)

    # Components
    found_components: set[str] = set()
    for comp, pattern in _COMPONENT_PATTERNS.items():
        if pattern.search(text):
            canonical = comp.upper() if comp.upper() in {"RGW", "RBD", "MDS", "OSD", "MON", "MGR", "NFS"} else comp
            found_components.add(canonical)

    text_lower = text.lower()
    for alias, canonical in _RE_COMPONENT_ALIASES.items():
        if alias in text_lower:
            found_components.add(canonical)

    info.components = sorted(found_components)

    # Error messages
    for match in _RE_ERROR_LINE.finditer(text):
        line = match.group(0).strip()
        if len(line) > 20 and line not in info.error_messages:
            info.error_messages.append(line[:500])

    # Stack traces
    for match in _RE_STACK_TRACE.finditer(text):
        trace = match.group(0).strip()
        if trace and trace not in info.stack_traces:
            info.stack_traces.append(trace[:2000])

    # Health warnings
    for match in _RE_HEALTH_WARNING.finditer(text):
        warning = match.group(1).strip()
        if warning not in info.health_warnings:
            info.health_warnings.append(warning)

    # Severity
    sev_match = _RE_SEVERITY.search(text)
    if sev_match:
        raw = sev_match.group(1).lower()
        info.severity = _SEVERITY_MAP.get(raw, raw)

    # JIRA labels
    for match in _RE_JIRA_LABEL.finditer(text):
        label = match.group(1)
        if label not in info.jira_labels:
            info.jira_labels.append(label)

    return info
