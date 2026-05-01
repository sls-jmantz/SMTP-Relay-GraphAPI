"""Configuration loading and network-policy helpers.

Config is read from a YAML file. Secrets (tenant id, client id, client secret)
may alternatively be supplied via environment variables so the image can be
run without baking secrets into the config file.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from typing import Iterable

import yaml


@dataclass
class AzureConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    # Graph authority + scope are effectively fixed for client-credentials,
    # but expose them so odd-cloud tenants (GCC High, 21Vianet) can override.
    authority: str = "https://login.microsoftonline.com"
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    scope: str = "https://graph.microsoft.com/.default"


@dataclass
class SmtpConfig:
    host: str = "0.0.0.0"
    port: int = 25
    hostname: str = "smtp-relay.local"
    # Max DATA size in bytes. Messages under ~3 MB go via Graph /sendMail as
    # a single MIME POST; larger ones are uploaded as a draft + chunked
    # attachment upload session. The upper bound here must stay at or below
    # the tenant's Exchange Online MaxSendSize (default 35 MB).
    max_message_size: int = 35 * 1024 * 1024


@dataclass
class RelayConfig:
    azure: AzureConfig
    smtp: SmtpConfig
    allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    allowed_senders: list[str] = field(default_factory=list)
    save_to_sent_items: bool = False
    log_level: str = "INFO"

    def is_ip_allowed(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.allowed_networks)

    def is_sender_allowed(self, address: str) -> bool:
        addr = (address or "").strip().lower()
        if not addr:
            return False
        for pattern in self.allowed_senders:
            p = pattern.strip().lower()
            if not p:
                continue
            if p == addr:
                return True
            # Allow a simple "*@domain.com" wildcard form.
            if p.startswith("*@") and addr.endswith(p[1:]):
                return True
        return False


def _parse_networks(raw: Iterable[str]) -> list:
    nets: list = []
    for entry in raw or []:
        entry = str(entry).strip()
        if not entry:
            continue
        # Accept bare IPs (treat as /32 or /128) as well as CIDRs.
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(f"Invalid CIDR/IP in allowed_networks: {entry!r}") from exc
    return nets


def _env_override(value: str | None, env_name: str) -> str | None:
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val
    return value


def load_config(path: str) -> RelayConfig:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    azure_raw = data.get("azure", {}) or {}
    tenant_id = _env_override(azure_raw.get("tenant_id"), "AZURE_TENANT_ID")
    client_id = _env_override(azure_raw.get("client_id"), "AZURE_CLIENT_ID")
    client_secret = _env_override(azure_raw.get("client_secret"), "AZURE_CLIENT_SECRET")

    missing = [
        name
        for name, val in (
            ("tenant_id", tenant_id),
            ("client_id", client_id),
            ("client_secret", client_secret),
        )
        if not val
    ]
    if missing:
        raise ValueError(
            "Missing required azure config value(s): "
            + ", ".join(missing)
            + ". Set them in config.yaml under 'azure' or via AZURE_TENANT_ID / "
            "AZURE_CLIENT_ID / AZURE_CLIENT_SECRET environment variables."
        )

    azure = AzureConfig(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        authority=azure_raw.get("authority", "https://login.microsoftonline.com"),
        graph_base_url=azure_raw.get("graph_base_url", "https://graph.microsoft.com/v1.0"),
        scope=azure_raw.get("scope", "https://graph.microsoft.com/.default"),
    )

    smtp_raw = data.get("smtp", {}) or {}
    smtp = SmtpConfig(
        host=smtp_raw.get("host", "0.0.0.0"),
        port=int(smtp_raw.get("port", 25)),
        hostname=smtp_raw.get("hostname", "smtp-relay.local"),
        max_message_size=int(smtp_raw.get("max_message_size", 35 * 1024 * 1024)),
    )

    return RelayConfig(
        azure=azure,
        smtp=smtp,
        allowed_networks=_parse_networks(data.get("allowed_networks", [])),
        allowed_senders=[str(s) for s in data.get("allowed_senders", []) or []],
        save_to_sent_items=bool(data.get("save_to_sent_items", False)),
        log_level=str(data.get("log_level", "INFO")).upper(),
    )
