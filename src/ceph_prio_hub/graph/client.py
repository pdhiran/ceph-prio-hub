"""Microsoft Graph API client for reading prio-list emails.

Uses device code flow for authentication (works well for CLI/MCP servers).
Tokens are cached to disk so re-authentication is rare.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ceph_prio_hub.config import (
    ALL_PRIO_ADDRESSES,
    CONFIG_DIR,
    GRAPH_SCOPES,
    PRIO_LISTS,
    TOKEN_CACHE_FILE,
    AzureConfig,
)
from ceph_prio_hub.graph.models import EmailAddress, EmailDetail, EmailSummary

logger = logging.getLogger(__name__)


class GraphAuthError(Exception):
    """Raised when Azure AD authentication fails or is not configured."""


class GraphClient:
    """Microsoft Graph API client for mail operations.

    Handles authentication via MSAL device code flow with persistent token caching.
    """

    def __init__(self, config: AzureConfig) -> None:
        self._config = config
        self._app = None
        self._account = None
        self._access_token: str | None = None

    def _get_msal_app(self):
        """Lazily create the MSAL public client application."""
        if self._app is not None:
            return self._app

        import msal

        cache = msal.SerializableTokenCache()
        if TOKEN_CACHE_FILE.exists():
            cache.deserialize(TOKEN_CACHE_FILE.read_text())

        self._app = msal.PublicClientApplication(
            client_id=self._config.client_id,
            authority=f"https://login.microsoftonline.com/{self._config.tenant_id}",
            token_cache=cache,
        )
        return self._app

    def _save_cache(self) -> None:
        app = self._get_msal_app()
        if app.token_cache.has_state_changed:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            TOKEN_CACHE_FILE.write_text(app.token_cache.serialize())

    def _acquire_token(self) -> str:
        """Acquire an access token, using cache or device code flow."""
        import msal

        app = self._get_msal_app()

        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
            if result and "access_token" in result:
                self._save_cache()
                return result["access_token"]

        flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
        if "user_code" not in flow:
            raise GraphAuthError(f"Device flow initiation failed: {flow.get('error_description', 'unknown error')}")

        print(f"\n{'='*60}")
        print(f"To authenticate with Microsoft Graph:")
        print(f"  1. Open: {flow['verification_uri']}")
        print(f"  2. Enter code: {flow['user_code']}")
        print(f"{'='*60}\n")

        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise GraphAuthError(f"Authentication failed: {result.get('error_description', 'unknown error')}")

        self._save_cache()
        return result["access_token"]

    def _get_headers(self) -> dict[str, str]:
        if not self._access_token:
            self._access_token = self._acquire_token()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _request(self, url: str, params: dict | None = None) -> dict:
        """Make an authenticated GET request to Graph API."""
        import requests

        try:
            resp = requests.get(url, headers=self._get_headers(), params=params, timeout=30)
        except requests.RequestException as exc:
            raise GraphAuthError(f"Graph API request failed: {exc}") from exc

        if resp.status_code == 401:
            self._access_token = self._acquire_token()
            resp = requests.get(url, headers=self._get_headers(), params=params, timeout=30)

        resp.raise_for_status()
        return resp.json()

    def _build_prio_filter(
        self,
        prio_list: str | None = None,
        days_back: int = 7,
        since: datetime | None = None,
    ) -> str:
        """Build an OData $filter for prio-list emails."""
        if prio_list and prio_list != "all":
            addresses = PRIO_LISTS.get(prio_list, [])
        else:
            addresses = ALL_PRIO_ADDRESSES

        if since:
            cutoff = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        addr_filters = []
        for addr in addresses:
            addr_filters.append(
                f"(toRecipients/any(r: r/emailAddress/address eq '{addr}') "
                f"or ccRecipients/any(r: r/emailAddress/address eq '{addr}'))"
            )

        addr_clause = " or ".join(addr_filters)
        return f"receivedDateTime ge {cutoff} and ({addr_clause})"

    def _parse_email_summary(self, msg: dict) -> EmailSummary:
        """Parse a Graph API message into an EmailSummary."""
        sender_data = msg.get("from", {}).get("emailAddress", {})
        return EmailSummary(
            message_id=msg.get("id", ""),
            conversation_id=msg.get("conversationId", ""),
            subject=msg.get("subject", ""),
            sender=EmailAddress(
                name=sender_data.get("name", ""),
                address=sender_data.get("address", ""),
            ),
            to_recipients=[
                EmailAddress(
                    name=r.get("emailAddress", {}).get("name", ""),
                    address=r.get("emailAddress", {}).get("address", ""),
                )
                for r in msg.get("toRecipients", [])
            ],
            cc_recipients=[
                EmailAddress(
                    name=r.get("emailAddress", {}).get("name", ""),
                    address=r.get("emailAddress", {}).get("address", ""),
                )
                for r in msg.get("ccRecipients", [])
            ],
            received_date=msg.get("receivedDateTime"),
            body_preview=msg.get("bodyPreview", ""),
            has_attachments=msg.get("hasAttachments", False),
            is_read=msg.get("isRead", False),
        )

    def _parse_email_detail(self, msg: dict) -> EmailDetail:
        """Parse a Graph API message into an EmailDetail with full body."""
        from bs4 import BeautifulSoup

        sender_data = msg.get("from", {}).get("emailAddress", {})
        body = msg.get("body", {})
        body_content = body.get("content", "")
        body_type = body.get("contentType", "text")

        if body_type.lower() == "html":
            body_html = body_content
            soup = BeautifulSoup(body_content, "html.parser")
            body_text = soup.get_text(separator="\n", strip=True)
        else:
            body_html = ""
            body_text = body_content

        attachments = msg.get("attachments", [])
        attachment_names = [a.get("name", "") for a in attachments if a.get("name")]

        return EmailDetail(
            message_id=msg.get("id", ""),
            conversation_id=msg.get("conversationId", ""),
            subject=msg.get("subject", ""),
            sender=EmailAddress(
                name=sender_data.get("name", ""),
                address=sender_data.get("address", ""),
            ),
            to_recipients=[
                EmailAddress(
                    name=r.get("emailAddress", {}).get("name", ""),
                    address=r.get("emailAddress", {}).get("address", ""),
                )
                for r in msg.get("toRecipients", [])
            ],
            cc_recipients=[
                EmailAddress(
                    name=r.get("emailAddress", {}).get("name", ""),
                    address=r.get("emailAddress", {}).get("address", ""),
                )
                for r in msg.get("ccRecipients", [])
            ],
            received_date=msg.get("receivedDateTime"),
            body_preview=msg.get("bodyPreview", ""),
            body_text=body_text,
            body_html=body_html,
            has_attachments=msg.get("hasAttachments", False),
            attachment_names=attachment_names,
            is_read=msg.get("isRead", False),
        )

    def fetch_prio_emails(
        self,
        prio_list: str | None = None,
        days_back: int = 7,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[EmailSummary]:
        """Fetch recent emails from prio-lists.

        Args:
            prio_list: Filter to a specific list ("ceph", "ocs", "odf") or None/all.
            days_back: How many days back to look (ignored if since is provided).
            limit: Maximum number of emails to return.
            since: Fetch emails received after this datetime (overrides days_back).
        """
        odata_filter = self._build_prio_filter(prio_list, days_back, since)

        url = "https://graph.microsoft.com/v1.0/me/messages"
        params = {
            "$filter": odata_filter,
            "$select": "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,hasAttachments,isRead",
            "$orderby": "receivedDateTime desc",
            "$top": str(min(limit, 100)),
        }

        emails: list[EmailSummary] = []
        while url and len(emails) < limit:
            data = self._request(url, params)
            for msg in data.get("value", []):
                emails.append(self._parse_email_summary(msg))
            url = data.get("@odata.nextLink")
            params = None

        return emails[:limit]

    def get_email_detail(self, message_id: str) -> EmailDetail:
        """Get full email details including body content."""
        url = f"https://graph.microsoft.com/v1.0/me/messages/{message_id}"
        params = {
            "$select": "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,body,hasAttachments,isRead",
            "$expand": "attachments($select=name,contentType,size)",
        }
        data = self._request(url, params)
        return self._parse_email_detail(data)

    def get_conversation_thread(self, conversation_id: str) -> list[EmailSummary]:
        """Get all emails in a conversation thread."""
        url = "https://graph.microsoft.com/v1.0/me/messages"
        params = {
            "$filter": f"conversationId eq '{conversation_id}'",
            "$select": "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,hasAttachments,isRead",
            "$orderby": "receivedDateTime asc",
            "$top": "100",
        }
        data = self._request(url, params)
        return [self._parse_email_summary(msg) for msg in data.get("value", [])]

    def search_emails(
        self,
        query: str,
        prio_list: str | None = None,
        days_back: int = 30,
        limit: int = 20,
    ) -> list[EmailSummary]:
        """Search prio-list emails by keyword using Graph API $search."""
        url = "https://graph.microsoft.com/v1.0/me/messages"

        params = {
            "$search": f'"{query}"',
            "$select": "id,conversationId,subject,from,toRecipients,ccRecipients,receivedDateTime,bodyPreview,hasAttachments,isRead",
            "$top": str(min(limit, 100)),
        }

        data = self._request(url, params)
        emails = [self._parse_email_summary(msg) for msg in data.get("value", [])]

        if prio_list and prio_list != "all":
            target_addrs = {a.lower() for a in PRIO_LISTS.get(prio_list, [])}
        else:
            target_addrs = {a.lower() for a in ALL_PRIO_ADDRESSES}

        filtered = []
        for email in emails:
            all_recipients = {r.address.lower() for r in email.to_recipients + email.cc_recipients}
            if all_recipients & target_addrs:
                filtered.append(email)

        return filtered[:limit]
