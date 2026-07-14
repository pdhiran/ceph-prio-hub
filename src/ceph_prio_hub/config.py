"""Configuration for prio-list addresses, state paths, and defaults."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".ceph-prio-hub"
STATE_DIR = CONFIG_DIR / "state"
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_CACHE_FILE = CONFIG_DIR / "token_cache.json"

PRIO_LISTS: dict[str, list[str]] = {
    "ceph": [
        "ceph-prio-list@redhat.com",
        "ceph-prio-list@wwpdl.vnet.ibm.com",
    ],
    "ocs": [
        "ocs-prio-list@redhat.com",
    ],
    "odf": [
        "odf-prio-list@wwpdl.vnet.ibm.com",
    ],
}

ALL_PRIO_ADDRESSES: list[str] = [
    addr for addrs in PRIO_LISTS.values() for addr in addrs
]

DEFAULT_DAYS_BACK = 7
DEFAULT_LIMIT = 50
DEFAULT_SYNC_INTERVAL_HOURS = 4

GRAPH_SCOPES = ["Mail.Read"]


@dataclass
class AzureConfig:
    """Azure AD app registration details for Microsoft Graph API access."""

    client_id: str = ""
    tenant_id: str = ""

    @classmethod
    def load(cls) -> "AzureConfig":
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text())
            return cls(
                client_id=data.get("client_id", ""),
                tenant_id=data.get("tenant_id", ""),
            )
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps({
            "client_id": self.client_id,
            "tenant_id": self.tenant_id,
        }, indent=2))

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.tenant_id)


@dataclass
class ServerConfig:
    azure: AzureConfig = field(default_factory=AzureConfig)
    state_dir: Path = STATE_DIR
    sync_interval_hours: float = DEFAULT_SYNC_INTERVAL_HOURS

    @classmethod
    def load(cls) -> "ServerConfig":
        azure = AzureConfig.load()
        return cls(azure=azure)

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
