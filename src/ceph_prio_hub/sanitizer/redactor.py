"""Redact customer-sensitive data from email content before publishing.

Ported from ceph-issue-kb/src/ceph_issue_kb/indexer/sanitizer.py and adapted
for the prio-list email data model. Uses deterministic [REDACTED-*-<hash>]
tokens so downstream deduplication and caching remain stable.
"""

from __future__ import annotations

import hashlib
import re

from ceph_prio_hub.sanitizer.allowlists import (
    is_allowed_domain,
    is_allowed_hostname,
    is_allowed_ip,
)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_RE_PRIVATE_IP = re.compile(
    r"\b("
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r")\b"
)

_RE_INTERNAL_DOMAIN = re.compile(
    r"[a-zA-Z0-9._-]+"
    r"\.(?:corp|internal|intra|priv|private|lan)\."
    r"[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

_RE_LOCAL_DOMAIN = re.compile(
    r"[a-zA-Z0-9._-]{3,}\.(?:localdomain|site|home\.arpa)\b",
    re.IGNORECASE,
)

_RE_CUSTOMER_NAME = re.compile(
    r"(?i)(customer\s+(?:affected|name|is|account)\s*[-:]\s*)"
    r"([A-Z][A-Za-z\s&.,()]{2,40})"
)

_RE_CASE_NUMBER_PROSE = re.compile(
    r"(?i)((?:case|ticket)\s*(?:number|no|id|#)?\s*[:# ]?\s*)(\d{7,8})\b"
)

_RE_EMAIL = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z][a-zA-Z0-9.-]*\.[a-zA-Z]{2,}\b"
)

_RE_NOT_EMAIL = re.compile(
    r"\.service$"
    r"|@\d+\."
    r"|@osd\."
    r"|@nvmeof\."
    r"|@mon\."
    r"|@mgr\."
    r"|@mds\."
    r"|@rgw\."
    r"|@nfs\."
    r"|@smb\."
    r"|@iscsi\."
    r"|@rbd-mirror\."
    r"|@ceph"
    r"|@node-exporter\."
    r"|@prometheus\."
    r"|@tty"
)

_RE_CREDENTIAL = re.compile(
    r"(?i)(password|secret|token|api[_-]?key|access[_-]?key)\s*[:=]\s*\S+",
)


def _hash8(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def sanitize_text(text: str) -> str:
    """Apply all redaction rules to a single string. Idempotent."""

    text = _RE_INTERNAL_DOMAIN.sub(
        lambda m: m.group(0)
        if is_allowed_domain(m.group(0)) or is_allowed_hostname(m.group(0))
        else f"[REDACTED-HOST-{_hash8(m.group(0))}]",
        text,
    )

    text = _RE_LOCAL_DOMAIN.sub(
        lambda m: m.group(0)
        if is_allowed_hostname(m.group(0))
        else f"[REDACTED-HOST-{_hash8(m.group(0))}]",
        text,
    )

    text = _RE_PRIVATE_IP.sub(
        lambda m: m.group(1)
        if is_allowed_ip(m.group(1))
        else f"[REDACTED-IP-{_hash8(m.group(1))}]",
        text,
    )

    text = _RE_CUSTOMER_NAME.sub(
        lambda m: f"{m.group(1)}[REDACTED-CUSTOMER-{_hash8(m.group(2).strip())}]",
        text,
    )

    text = _RE_CASE_NUMBER_PROSE.sub(
        lambda m: f"{m.group(1)}[REDACTED-CASE-{_hash8(m.group(2))}]",
        text,
    )

    def _email_replacer(m: re.Match) -> str:
        email = m.group(0)
        if _RE_NOT_EMAIL.search(email):
            return email
        domain = email.split("@", 1)[1]
        if domain[0].isdigit():
            return email
        if is_allowed_domain(domain):
            return email
        return f"[REDACTED-EMAIL-{_hash8(email)}]"

    text = _RE_EMAIL.sub(_email_replacer, text)

    text = _RE_CREDENTIAL.sub(
        lambda m: f"{m.group(1)}: [REDACTED-CREDENTIAL]",
        text,
    )

    return text


def sanitize_dict(data: dict, skip_keys: frozenset[str] | None = None) -> dict:
    """Recursively sanitize all string values in a dict.

    Args:
        data: Dictionary to sanitize (not modified in place).
        skip_keys: Keys whose values should NOT be sanitized (e.g. "case_ids",
                   "issue_id" — structural fields needed for tracking).
    """
    if skip_keys is None:
        skip_keys = frozenset({"issue_id", "case_ids", "jira_ids", "message_id", "conversation_id"})

    result = {}
    for key, value in data.items():
        if key in skip_keys:
            result[key] = value
        elif isinstance(value, str):
            result[key] = sanitize_text(value)
        elif isinstance(value, dict):
            result[key] = sanitize_dict(value, skip_keys)
        elif isinstance(value, list):
            result[key] = [
                sanitize_dict(item, skip_keys) if isinstance(item, dict)
                else sanitize_text(item) if isinstance(item, str)
                else item
                for item in value
            ]
        else:
            result[key] = value
    return result
