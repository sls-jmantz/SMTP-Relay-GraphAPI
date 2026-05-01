"""Parse an incoming RFC 5322 MIME blob into the pieces Graph's draft-message
API wants: JSON metadata for the message itself, plus a list of attachment
bodies we can upload separately.

This is only used when the message is too large for Graph's one-shot
/sendMail MIME endpoint (~3 MB raw). For the common case the raw MIME is
forwarded verbatim and we never touch this module.
"""

from __future__ import annotations

import email
import email.policy
import mimetypes
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from typing import Iterable


@dataclass
class Attachment:
    name: str
    content_type: str
    content_bytes: bytes
    # For inline images referenced from the HTML body via cid:.
    content_id: str | None = None
    is_inline: bool = False

    @property
    def size(self) -> int:
        return len(self.content_bytes)


@dataclass
class ParsedMessage:
    subject: str = ""
    # Body is chosen as HTML if present, else plain text. Graph wants a single
    # body, so if both exist we pick HTML and drop plain. (Graph draft API
    # only supports one body; MIME multipart/alternative has to collapse.)
    body_content: str = ""
    body_content_type: str = "Text"  # "Text" or "HTML"
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    reply_to: list[str] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
    # Any Subject/References/In-Reply-To-ish headers we want to preserve on
    # the Graph message (Graph supports internetMessageHeaders for X-* only,
    # but stock headers like Message-ID are set automatically).
    extra_headers: list[tuple[str, str]] = field(default_factory=list)


def _addrs(header_values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for _, addr in getaddresses(list(header_values)):
        addr = (addr or "").strip()
        if addr:
            out.append(addr)
    return out


def _decode_bytes(part: EmailMessage) -> bytes:
    """Return the part's decoded payload bytes regardless of CTE."""
    payload = part.get_payload(decode=True)
    if payload is None:
        # Rare: multipart container slipped in, or a pathological message.
        raw = part.get_payload()
        if isinstance(raw, str):
            return raw.encode("utf-8", errors="replace")
        return b""
    return payload


def _decode_text(part: EmailMessage) -> str:
    charset = part.get_content_charset() or "utf-8"
    data = _decode_bytes(part)
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _strip_angles(cid: str | None) -> str | None:
    if not cid:
        return None
    cid = cid.strip()
    if cid.startswith("<") and cid.endswith(">"):
        cid = cid[1:-1]
    return cid or None


def _looks_like_attachment(part: EmailMessage) -> bool:
    """True if this leaf part should be treated as a file attachment."""
    disp = (part.get_content_disposition() or "").lower()
    if disp == "attachment":
        return True
    if part.get_filename():
        return True
    ctype = part.get_content_type().lower()
    if disp == "inline" and not ctype.startswith("text/"):
        return True
    # Anything that isn't text/plain or text/html and isn't a container is
    # almost certainly a binary payload we should preserve as an attachment.
    if not ctype.startswith("text/"):
        return True
    return False


def _synthetic_filename(part: EmailMessage, index: int) -> str:
    ctype = part.get_content_type()
    ext = mimetypes.guess_extension(ctype) or ".bin"
    return f"attachment-{index}{ext}"


def parse_mime(raw: bytes) -> ParsedMessage:
    """Walk the MIME tree and extract body + attachments.

    Strategy:
      - Prefer the first text/html leaf for the body. If none, fall back to
        the first text/plain leaf.
      - Everything else that isn't a multipart container is emitted as an
        attachment (with a synthesized filename if none was given).
      - multipart/alternative: we pick HTML > text as above.
      - multipart/related: the non-root inline parts become attachments with
        their Content-ID preserved, so HTML cid: references keep working.
    """
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)

    parsed = ParsedMessage()
    parsed.subject = msg.get("Subject", "") or ""
    parsed.to = _addrs(msg.get_all("To", []) or [])
    parsed.cc = _addrs(msg.get_all("Cc", []) or [])
    parsed.bcc = _addrs(msg.get_all("Bcc", []) or [])
    parsed.reply_to = _addrs(msg.get_all("Reply-To", []) or [])

    # Preserve any X-* headers the device set; Graph supports those verbatim.
    for name, value in msg.items():
        if name.lower().startswith("x-"):
            parsed.extra_headers.append((name, value))

    text_body: str | None = None
    html_body: str | None = None

    att_index = 0
    for part in msg.walk():
        if part.is_multipart():
            continue

        ctype = part.get_content_type().lower()
        disp = (part.get_content_disposition() or "").lower()

        # Body candidates: text/* parts that aren't attachments.
        if (
            ctype in ("text/plain", "text/html")
            and disp != "attachment"
            and not part.get_filename()
        ):
            text_value = _decode_text(part)
            if ctype == "text/html" and html_body is None:
                html_body = text_value
                continue
            if ctype == "text/plain" and text_body is None:
                text_body = text_value
                continue
            # Duplicate body part of same type: ignore (first wins).
            continue

        if _looks_like_attachment(part):
            att_index += 1
            name = part.get_filename() or _synthetic_filename(part, att_index)
            parsed.attachments.append(
                Attachment(
                    name=name,
                    content_type=ctype or "application/octet-stream",
                    content_bytes=_decode_bytes(part),
                    content_id=_strip_angles(part.get("Content-ID")),
                    is_inline=(disp == "inline"),
                )
            )

    if html_body is not None:
        parsed.body_content = html_body
        parsed.body_content_type = "HTML"
    elif text_body is not None:
        parsed.body_content = text_body
        parsed.body_content_type = "Text"
    else:
        parsed.body_content = ""
        parsed.body_content_type = "Text"

    return parsed


def to_graph_message(
    parsed: ParsedMessage,
    sender: str,
    envelope_rcpts: list[str] | None = None,
) -> dict:
    """Produce the JSON body for POST /users/{id}/messages (draft create).

    `sender` becomes the From address (Graph will validate it against the
    mailbox the token was granted access to). We merge envelope recipients
    into To if the headers don't already cover them, so the recipient list
    Exchange uses for routing matches what the device asked for.
    """
    # Graph expects recipient objects: {"emailAddress": {"address": "..."}}
    def _emails(addrs: list[str]) -> list[dict]:
        return [{"emailAddress": {"address": a}} for a in addrs if a]

    to_addrs = list(parsed.to)
    if envelope_rcpts:
        seen = {a.lower() for a in (parsed.to + parsed.cc + parsed.bcc)}
        for rcpt in envelope_rcpts:
            if rcpt and rcpt.lower() not in seen:
                to_addrs.append(rcpt)

    body = {
        "subject": parsed.subject or "(no subject)",
        "body": {
            "contentType": parsed.body_content_type,
            "content": parsed.body_content,
        },
        "from": {"emailAddress": {"address": sender}},
        "sender": {"emailAddress": {"address": sender}},
        "toRecipients": _emails(to_addrs),
        "ccRecipients": _emails(parsed.cc),
        "bccRecipients": _emails(parsed.bcc),
    }
    if parsed.reply_to:
        body["replyTo"] = _emails(parsed.reply_to)
    if parsed.extra_headers:
        body["internetMessageHeaders"] = [
            {"name": n, "value": v} for n, v in parsed.extra_headers
        ]
    return body


def make_boundary() -> str:  # pragma: no cover - trivial
    return uuid.uuid4().hex
