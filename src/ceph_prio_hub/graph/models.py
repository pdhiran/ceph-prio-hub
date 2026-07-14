"""Pydantic models for email data from Microsoft Graph API."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class EmailAddress(BaseModel):
    name: str = ""
    address: str = ""


class EmailSummary(BaseModel):
    """Lightweight email representation for list views."""

    message_id: str = ""
    conversation_id: str = ""
    subject: str = ""
    sender: EmailAddress = Field(default_factory=EmailAddress)
    to_recipients: list[EmailAddress] = Field(default_factory=list)
    cc_recipients: list[EmailAddress] = Field(default_factory=list)
    received_date: datetime | None = None
    body_preview: str = ""
    has_attachments: bool = False
    is_read: bool = False

    @property
    def prio_lists(self) -> list[str]:
        """Return which prio-list addresses appear in To/Cc."""
        from ceph_prio_hub.config import ALL_PRIO_ADDRESSES
        all_recipients = [
            r.address.lower()
            for r in self.to_recipients + self.cc_recipients
        ]
        return [
            addr for addr in ALL_PRIO_ADDRESSES
            if addr.lower() in all_recipients
        ]


class EmailDetail(BaseModel):
    """Full email with body content."""

    message_id: str = ""
    conversation_id: str = ""
    subject: str = ""
    sender: EmailAddress = Field(default_factory=EmailAddress)
    to_recipients: list[EmailAddress] = Field(default_factory=list)
    cc_recipients: list[EmailAddress] = Field(default_factory=list)
    received_date: datetime | None = None
    body_preview: str = ""
    body_text: str = ""
    body_html: str = ""
    has_attachments: bool = False
    attachment_names: list[str] = Field(default_factory=list)
    is_read: bool = False
