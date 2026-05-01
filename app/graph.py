"""Microsoft Graph client for sending mail via the client-credentials flow.

Two send paths:

1. **One-shot MIME** (`POST /users/{id}/sendMail`, Content-Type: text/plain,
   body = base64(raw MIME)). Simple, one round trip. Graph caps this at
   ~4 MB of base64 body, i.e. ~3 MB raw MIME.

2. **Draft + upload session** (for anything larger). We parse the incoming
   MIME, create a draft via `POST /users/{id}/messages`, attach each part
   either inline (<3 MB, JSON) or via a resumable upload session (>=3 MB,
   chunked PUT), then `POST .../send` the draft. Per-attachment ceiling is
   ~150 MB; total message is bounded by the tenant's Exchange Online
   MaxSendSize (default 35 MB, admin-configurable up to 150 MB).

Auth: OAuth2 client-credentials directly against the tenant's v2.0 token
endpoint. We deliberately avoid MSAL because (a) our needs are minimal,
(b) MSAL requires https authorities which makes local testing painful, and
(c) fewer dependencies = smaller supply-chain surface.

Docs:
  https://learn.microsoft.com/graph/api/user-sendmail
  https://learn.microsoft.com/graph/api/user-post-messages
  https://learn.microsoft.com/graph/api/attachment-createuploadsession
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass

import httpx

from .config import AzureConfig
from .mime_split import ParsedMessage, parse_mime, to_graph_message


log = logging.getLogger("relay.graph")


class GraphError(Exception):
    """Raised when Graph or the token endpoint returns a non-success response."""


# Raw-MIME cutoff for the one-shot /sendMail path. Graph accepts up to ~4 MB
# of base64-encoded body; base64 inflates 4/3, so ~3 MB of raw MIME is the
# safe ceiling. We use slightly less to leave headroom for the POST framing.
_ONESHOT_MIME_MAX_BYTES = 3 * 1024 * 1024  # 3 MiB raw

# Per-attachment cutoff for using JSON POST vs upload session.
_INLINE_ATTACHMENT_MAX_BYTES = 3 * 1024 * 1024  # 3 MiB

# Chunk size for the upload session. Must be a multiple of 320 KiB per the
# Graph upload protocol. 12 * 320 KiB = 3840 KiB is the largest multiple of
# 320 KiB that stays under the documented 4 MB per-chunk cap.
_UPLOAD_CHUNK_SIZE = 12 * 320 * 1024  # 3,932,160 bytes


@dataclass
class _CachedToken:
    value: str
    expires_at: float  # unix epoch seconds


class GraphClient:
    """Thread-safe Graph client with in-memory token caching."""

    _EXPIRY_SKEW_SECONDS = 60

    def __init__(self, azure: AzureConfig, http_timeout: float = 60.0) -> None:
        self._azure = azure
        self._token_url = (
            f"{azure.authority.rstrip('/')}/{azure.tenant_id}/oauth2/v2.0/token"
        )
        self._graph_base = azure.graph_base_url.rstrip("/")
        self._lock = threading.Lock()
        self._token: _CachedToken | None = None
        # Upload-session PUTs can be slow on large files; use a generous
        # timeout here. Token + JSON calls are fast; httpx per-call override
        # isn't necessary.
        self._http = httpx.Client(timeout=http_timeout)

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # pragma: no cover - best-effort
            pass

    # ---- token handling -------------------------------------------------

    def _get_token(self) -> str:
        with self._lock:
            now = time.time()
            if self._token and self._token.expires_at - self._EXPIRY_SKEW_SECONDS > now:
                return self._token.value

            log.debug("Acquiring new Graph token from %s", self._token_url)
            try:
                resp = self._http.post(
                    self._token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._azure.client_id,
                        "client_secret": self._azure.client_secret,
                        "scope": self._azure.scope,
                    },
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise GraphError(f"HTTP error contacting token endpoint: {exc}") from exc

            if resp.status_code != 200:
                raise GraphError(
                    f"Token endpoint returned HTTP {resp.status_code}: {self._body_for_error(resp)!r}"
                )

            try:
                payload = resp.json()
            except ValueError as exc:
                raise GraphError(f"Token endpoint returned non-JSON: {exc}") from exc

            access_token = payload.get("access_token")
            expires_in = int(payload.get("expires_in") or 3600)
            if not access_token:
                raise GraphError(f"Token endpoint response missing access_token: {payload!r}")

            self._token = _CachedToken(value=access_token, expires_at=now + expires_in)
            return access_token

    def _auth_headers(self, extra: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        if extra:
            headers.update(extra)
        return headers

    def _invalidate_token_on_401(self, status_code: int) -> None:
        if status_code == 401:
            with self._lock:
                self._token = None

    @staticmethod
    def _body_for_error(resp: httpx.Response):
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ---- public entry ---------------------------------------------------

    def send(
        self,
        sender: str,
        mime_bytes: bytes,
        envelope_rcpts: list[str] | None = None,
        save_to_sent_items: bool = False,
    ) -> None:
        """Send a message, picking the one-shot or draft/upload path by size."""
        if len(mime_bytes) <= _ONESHOT_MIME_MAX_BYTES:
            self._send_oneshot_mime(sender, mime_bytes, save_to_sent_items)
            return

        log.info(
            "Message from %s is %d bytes (>%d); using draft + upload-session path",
            sender,
            len(mime_bytes),
            _ONESHOT_MIME_MAX_BYTES,
        )
        parsed = parse_mime(mime_bytes)
        self._send_via_draft(sender, parsed, envelope_rcpts, save_to_sent_items)

    # ---- path 1: one-shot MIME -----------------------------------------

    def _send_oneshot_mime(
        self, sender: str, mime_bytes: bytes, save_to_sent_items: bool
    ) -> None:
        url = (
            f"{self._graph_base}/users/{sender}/sendMail"
            f"?saveToSentItems={'true' if save_to_sent_items else 'false'}"
        )
        headers = self._auth_headers({"Content-Type": "text/plain"})
        body = base64.b64encode(mime_bytes)

        try:
            resp = self._http.post(url, headers=headers, content=body)
        except httpx.HTTPError as exc:
            raise GraphError(f"HTTP error talking to Graph /sendMail: {exc}") from exc

        if resp.status_code == 202:
            log.info(
                "Graph /sendMail accepted mail as %s (size=%d bytes)",
                sender,
                len(mime_bytes),
            )
            return

        self._invalidate_token_on_401(resp.status_code)
        raise GraphError(
            f"Graph /sendMail failed for {sender}: HTTP {resp.status_code} "
            f"{self._body_for_error(resp)!r}"
        )

    # ---- path 2: draft + upload session --------------------------------

    def _send_via_draft(
        self,
        sender: str,
        parsed: ParsedMessage,
        envelope_rcpts: list[str] | None,
        save_to_sent_items: bool,
    ) -> None:
        message_body = to_graph_message(parsed, sender, envelope_rcpts)
        message_id = self._create_draft(sender, message_body)
        log.info(
            "Created draft %s for %s (%d attachment(s))",
            message_id,
            sender,
            len(parsed.attachments),
        )

        try:
            for att in parsed.attachments:
                if att.size <= _INLINE_ATTACHMENT_MAX_BYTES:
                    self._attach_inline(sender, message_id, att)
                else:
                    self._attach_via_upload_session(sender, message_id, att)
            self._send_draft(sender, message_id, save_to_sent_items)
        except Exception:
            # Best-effort cleanup so failed drafts don't linger in the
            # sender's mailbox drafts folder.
            log.warning("Send failed for draft %s; attempting to delete", message_id)
            try:
                self._delete_draft(sender, message_id)
            except Exception as exc:  # pragma: no cover - logging only
                log.warning("Failed to delete draft %s: %s", message_id, exc)
            raise

    def _create_draft(self, sender: str, message_body: dict) -> str:
        url = f"{self._graph_base}/users/{sender}/messages"
        headers = self._auth_headers({"Content-Type": "application/json"})
        try:
            resp = self._http.post(url, headers=headers, json=message_body)
        except httpx.HTTPError as exc:
            raise GraphError(f"HTTP error creating draft: {exc}") from exc

        if resp.status_code not in (200, 201):
            self._invalidate_token_on_401(resp.status_code)
            raise GraphError(
                f"Draft create failed for {sender}: HTTP {resp.status_code} "
                f"{self._body_for_error(resp)!r}"
            )
        try:
            msg_id = resp.json().get("id")
        except ValueError:
            msg_id = None
        if not msg_id:
            raise GraphError(f"Draft create response missing id: {resp.text!r}")
        return msg_id

    def _attach_inline(self, sender: str, message_id: str, att) -> None:
        url = f"{self._graph_base}/users/{sender}/messages/{message_id}/attachments"
        headers = self._auth_headers({"Content-Type": "application/json"})
        body = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": att.name,
            "contentType": att.content_type,
            "contentBytes": base64.b64encode(att.content_bytes).decode("ascii"),
            "isInline": bool(att.is_inline),
        }
        if att.content_id:
            body["contentId"] = att.content_id

        try:
            resp = self._http.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise GraphError(f"HTTP error POSTing attachment {att.name!r}: {exc}") from exc

        if resp.status_code not in (200, 201):
            self._invalidate_token_on_401(resp.status_code)
            raise GraphError(
                f"Attachment POST failed for {att.name!r}: HTTP {resp.status_code} "
                f"{self._body_for_error(resp)!r}"
            )
        log.debug(
            "Attached %r (%d bytes, inline) to draft %s",
            att.name,
            att.size,
            message_id,
        )

    def _attach_via_upload_session(self, sender: str, message_id: str, att) -> None:
        # 1. Create the session.
        create_url = (
            f"{self._graph_base}/users/{sender}/messages/{message_id}"
            f"/attachments/createUploadSession"
        )
        headers = self._auth_headers({"Content-Type": "application/json"})
        session_body = {
            "AttachmentItem": {
                "attachmentType": "file",
                "name": att.name,
                "size": att.size,
                "contentType": att.content_type,
                "isInline": bool(att.is_inline),
            }
        }
        if att.content_id:
            session_body["AttachmentItem"]["contentId"] = att.content_id

        try:
            resp = self._http.post(create_url, headers=headers, json=session_body)
        except httpx.HTTPError as exc:
            raise GraphError(
                f"HTTP error creating upload session for {att.name!r}: {exc}"
            ) from exc

        if resp.status_code not in (200, 201):
            self._invalidate_token_on_401(resp.status_code)
            raise GraphError(
                f"createUploadSession failed for {att.name!r}: HTTP {resp.status_code} "
                f"{self._body_for_error(resp)!r}"
            )
        try:
            upload_url = resp.json().get("uploadUrl")
        except ValueError:
            upload_url = None
        if not upload_url:
            raise GraphError(
                f"createUploadSession response missing uploadUrl: {resp.text!r}"
            )

        # 2. PUT chunks. The upload URL is pre-signed; do NOT send the bearer
        # token with these requests (doing so causes Graph to reject them).
        total = att.size
        offset = 0
        data = att.content_bytes  # already decoded bytes
        while offset < total:
            end = min(offset + _UPLOAD_CHUNK_SIZE, total)
            chunk = data[offset:end]
            content_range = f"bytes {offset}-{end - 1}/{total}"
            put_headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": content_range,
            }
            try:
                put_resp = self._http.put(
                    upload_url, headers=put_headers, content=chunk
                )
            except httpx.HTTPError as exc:
                raise GraphError(
                    f"HTTP error uploading chunk {content_range} of {att.name!r}: {exc}"
                ) from exc

            if put_resp.status_code in (200, 201):
                # Final chunk: server signals success with 200/201.
                log.debug(
                    "Uploaded final chunk %s of %r to draft %s",
                    content_range,
                    att.name,
                    message_id,
                )
                offset = end
                break
            if put_resp.status_code == 202:
                # Intermediate chunk accepted; continue.
                offset = end
                continue
            raise GraphError(
                f"Chunk PUT failed for {att.name!r} range={content_range}: "
                f"HTTP {put_resp.status_code} {self._body_for_error(put_resp)!r}"
            )

        if offset < total:
            raise GraphError(
                f"Upload of {att.name!r} ended early at {offset}/{total} bytes"
            )
        log.info(
            "Uploaded %r (%d bytes) via upload session to draft %s",
            att.name,
            att.size,
            message_id,
        )

    def _send_draft(self, sender: str, message_id: str, save_to_sent_items: bool) -> None:
        url = f"{self._graph_base}/users/{sender}/messages/{message_id}/send"
        headers = self._auth_headers()
        # The /send endpoint has no saveToSentItems query param; drafts that
        # were created via /messages always get a Sent Items copy by default
        # unless the mailbox is configured otherwise. We keep the flag for
        # future use / parity with the one-shot path.
        _ = save_to_sent_items

        try:
            resp = self._http.post(url, headers=headers)
        except httpx.HTTPError as exc:
            raise GraphError(f"HTTP error sending draft {message_id}: {exc}") from exc

        if resp.status_code == 202:
            log.info("Graph sent draft %s as %s", message_id, sender)
            return

        self._invalidate_token_on_401(resp.status_code)
        raise GraphError(
            f"Draft send failed for {sender} ({message_id}): HTTP {resp.status_code} "
            f"{self._body_for_error(resp)!r}"
        )

    def _delete_draft(self, sender: str, message_id: str) -> None:
        url = f"{self._graph_base}/users/{sender}/messages/{message_id}"
        headers = self._auth_headers()
        resp = self._http.delete(url, headers=headers)
        if resp.status_code not in (200, 204):
            self._invalidate_token_on_401(resp.status_code)
            raise GraphError(
                f"Draft delete failed for {message_id}: HTTP {resp.status_code} "
                f"{self._body_for_error(resp)!r}"
            )
