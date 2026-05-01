"""aiosmtpd handler: enforce IP + sender policy, then relay via Graph."""

from __future__ import annotations

import email
import logging
from email.utils import getaddresses, parseaddr

from aiosmtpd.smtp import SMTP as SMTPServer, Envelope, Session

from .config import RelayConfig
from .graph import GraphClient, GraphError


log = logging.getLogger("relay.smtp")


def _peer_ip(session: Session) -> str:
    peer = session.peer
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return str(peer)


class RelayHandler:
    """aiosmtpd handler implementing the policy + Graph bridge."""

    def __init__(self, config: RelayConfig, graph: GraphClient) -> None:
        self.config = config
        self.graph = graph

    # ---- connection gate ------------------------------------------------

    async def handle_RCPT(
        self,
        server: SMTPServer,
        session: Session,
        envelope: Envelope,
        address: str,
        rcpt_options: list[str],
    ) -> str:
        ip = _peer_ip(session)
        if not self.config.is_ip_allowed(ip):
            log.warning("RCPT from disallowed IP %s rejected", ip)
            return "550 5.7.1 Your IP is not permitted to relay through this server"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_MAIL(
        self,
        server: SMTPServer,
        session: Session,
        envelope: Envelope,
        address: str,
        mail_options: list[str],
    ) -> str:
        ip = _peer_ip(session)
        if not self.config.is_ip_allowed(ip):
            log.warning("MAIL from disallowed IP %s rejected", ip)
            return "550 5.7.1 Your IP is not permitted to relay through this server"

        sender = (address or "").strip()
        if not self.config.is_sender_allowed(sender):
            log.warning("Sender %r from %s not in sender allowlist", sender, ip)
            return "550 5.7.1 Sender address not permitted"

        envelope.mail_from = sender
        envelope.mail_options.extend(mail_options)
        return "250 OK"

    # ---- payload --------------------------------------------------------

    async def handle_DATA(
        self,
        server: SMTPServer,
        session: Session,
        envelope: Envelope,
    ) -> str:
        ip = _peer_ip(session)
        mail_from = envelope.mail_from or ""
        rcpts = list(envelope.rcpt_tos or [])

        # content can be bytes or str depending on the client / aiosmtpd mode.
        raw = envelope.content
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="surrogateescape")

        if len(raw) > self.config.smtp.max_message_size:
            log.warning(
                "Message from %s (%s) exceeds max size %d (got %d)",
                mail_from,
                ip,
                self.config.smtp.max_message_size,
                len(raw),
            )
            return "552 5.3.4 Message exceeds maximum permitted size"

        # Cross-check the header From against the envelope MAIL FROM. We send
        # "as" the envelope sender via Graph (the URL path), so we also require
        # the header From to belong to the allowlist to avoid spoofing.
        try:
            msg = email.message_from_bytes(raw)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("Failed to parse incoming message from %s: %s", ip, exc)
            return "550 5.6.0 Malformed message"

        header_from_raw = msg.get("From", "")
        _, header_from_addr = parseaddr(header_from_raw)
        if header_from_addr and not self.config.is_sender_allowed(header_from_addr):
            log.warning(
                "Header From %r not allowed (envelope=%s, ip=%s)",
                header_from_addr,
                mail_from,
                ip,
            )
            return "550 5.7.1 From header address not permitted"

        # Envelope recipients should appear somewhere in To/Cc/Bcc; we don't
        # enforce that, we just log it for troubleshooting.
        hdr_rcpts = [
            addr for _, addr in getaddresses(
                msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Bcc", [])
            )
            if addr
        ]
        log.info(
            "Accepted DATA from ip=%s envelope_from=%s envelope_rcpts=%s header_rcpts=%s size=%d",
            ip,
            mail_from,
            rcpts,
            hdr_rcpts,
            len(raw),
        )

        try:
            self.graph.send(
                sender=mail_from,
                mime_bytes=raw,
                envelope_rcpts=rcpts,
                save_to_sent_items=self.config.save_to_sent_items,
            )
        except GraphError as exc:
            log.error("Graph relay failed for %s: %s", mail_from, exc)
            # 451 = transient; most MTAs / printers will retry. Use 554 for
            # clearly permanent-looking failures (auth, policy) if you prefer.
            return "451 4.7.0 Upstream relay failed; try again later"
        except Exception as exc:  # pragma: no cover - catch-all to keep the server alive
            log.exception("Unexpected error relaying mail from %s: %s", mail_from, exc)
            return "451 4.3.0 Internal relay error"

        return "250 2.0.0 Message accepted for delivery"
