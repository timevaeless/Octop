"""WhatsApp Cloud API channel.

Implements :class:`octop_gateway.channel.BaseChannel` so the extension can be
injected into ``octop_gateway.channels.BUILTIN_CHANNELS`` at runtime without
touching the upstream ``octop-gateway`` package.

Inbound path: Meta delivers webhooks to the octop HTTP callback route
(``webhook.py``), which verifies the ``X-Hub-Signature-256`` signature and
enqueues each raw message dict onto this channel's worker queue. Outbound
path: Graph API ``/{phone_number_id}/messages``.

Known platform constraint: free-form business-initiated messages are only
allowed within 24 hours of the user's last message. Replies inside an inbound
turn are always in-window; proactive ``push_*`` calls check the window and are
dropped with a warning when expired (template-message re-engagement is out of
scope for this extension).
"""

from __future__ import annotations

import logging
import time
import uuid as _uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

# upstream octop_gateway.channel lacks ChannelConstraints in __all__
from octop_gateway.channel import (  # type: ignore[attr-defined]
    BaseChannel,
    ChannelConfig,
    ChannelConstraints,
    MessageProcessor,
)
from octop_gateway.models import (
    AudioContent,
    ChannelSubject,
    ContentPart,
    FileContent,
    ImageContent,
    InboundMessage,
    TextContent,
    VideoContent,
)

from octop.infra.gateway.whatsapp.api import (
    DEFAULT_GRAPH_URL,
    MAX_AUDIO_BYTES,
    MAX_DOCUMENT_BYTES,
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    MEDIA_DOWNLOAD_HOSTS,
    WhatsAppApiError,
    resolve_media_url,
    send_message,
    upload_media,
    verify_credentials,
)

logger = logging.getLogger(__name__)

WHATSAPP_CHANNEL_TYPE = "whatsapp"

#: Meta free-form service window for business-initiated messages (seconds).
_SERVICE_WINDOW_SECONDS = 24 * 3600

#: Outbound text ceiling per message (Cloud API hard limit).
_MAX_TEXT_LENGTH = 4096

#: URL marker for media parts that still need id → URL resolution.
_MEDIA_URL_PREFIX = "wa-media://"


@dataclass
class WhatsAppConfig(ChannelConfig):
    """Configuration for the WhatsApp Cloud API channel.

    ``verify_token`` and ``app_secret`` are functionally required: without
    them the webhook endpoint cannot complete Meta's subscription handshake
    or verify event signatures, so there would be no inbound path at all.
    """

    phone_number_id: str = ""
    access_token: str = ""
    verify_token: str = ""
    app_secret: str = ""
    graph_url: str = ""

    required_credentials = ("phone_number_id", "access_token", "verify_token", "app_secret")

    def effective_graph_url(self) -> str:
        return (self.graph_url or "").strip() or DEFAULT_GRAPH_URL


class WhatsAppChannel(BaseChannel):
    """WhatsApp Cloud API channel (webhook inbound, Graph API outbound)."""

    channel_type = WHATSAPP_CHANNEL_TYPE

    def __init__(
        self,
        processor: MessageProcessor,
        *,
        config: WhatsAppConfig,
        channel_id: str | None = None,
        tenant_id: str | None = None,
        debounce_seconds: float = 0.0,
        constraints: ChannelConstraints | None = None,
    ) -> None:
        self._config = config
        super().__init__(
            processor,
            channel_id=channel_id,
            tenant_id=tenant_id,
            debounce_seconds=debounce_seconds,
            constraints=constraints,
            config=config,
        )
        self._connection_session: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _default_constraints(self) -> ChannelConstraints:
        return ChannelConstraints(
            send_rate_limit=(30, 60.0),
            show_thinking=False,
            show_tool_hints=True,
        )

    async def start(self) -> None:
        """Validate credentials against the Graph API, then go passive.

        Inbound delivery is webhook-driven, so ``start`` performs one cheap
        ``GET /{phone_number_id}`` (which doubles as the credential probe)
        and keeps no long-lived connection open.
        """
        self._connection_session = f"whatsapp-{_uuid.uuid4().hex[:12]}"
        http = await self._ensure_http()
        try:
            await verify_credentials(
                http,
                self._config.effective_graph_url(),
                self._config.phone_number_id,
                self._config.access_token,
            )
        except Exception:
            self._connection_session = None
            raise
        logger.info("WhatsAppChannel started (phone_number_id=%s)", self._config.phone_number_id)

    async def stop(self) -> None:
        self._connection_session = None
        await self._close_http()

    # ------------------------------------------------------------------
    # Outbound (platform primitives)
    # ------------------------------------------------------------------

    @staticmethod
    def _media_limit(media: ContentPart) -> int:
        if isinstance(media, ImageContent):
            return MAX_IMAGE_BYTES
        if isinstance(media, VideoContent):
            return MAX_VIDEO_BYTES
        if isinstance(media, AudioContent):
            return MAX_AUDIO_BYTES
        return MAX_DOCUMENT_BYTES

    @staticmethod
    def _media_kind(media: ContentPart) -> str:
        if isinstance(media, ImageContent):
            return "image"
        if isinstance(media, VideoContent):
            return "video"
        if isinstance(media, AudioContent):
            return "audio"
        return "document"

    @staticmethod
    def _chunk_text(text: str) -> list[str]:
        if len(text) <= _MAX_TEXT_LENGTH:
            return [text]
        chunks: list[str] = []
        while text:
            chunks.append(text[:_MAX_TEXT_LENGTH])
            text = text[_MAX_TEXT_LENGTH:]
        return chunks

    def _recipient(self, subject: ChannelSubject) -> str:
        return str(subject.metadata.get("wa_id") or subject.subject_id or "")

    def _text_payload(self, wa_id: str, text: str) -> dict[str, Any]:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": wa_id,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }

    async def _send_text(self, subject: ChannelSubject, text: str) -> None:
        wa_id = self._recipient(subject)
        if not wa_id or not text.strip():
            return
        http = await self._ensure_http()
        for chunk in self._chunk_text(text):
            await send_message(
                http,
                self._config.effective_graph_url(),
                self._config.phone_number_id,
                self._config.access_token,
                self._text_payload(wa_id, chunk),
            )

    async def _send_content(self, subject: ChannelSubject, parts: list[ContentPart]) -> None:
        for part in parts:
            if isinstance(part, TextContent):
                await self._send_text(subject, part.text)
            else:
                await self._send_media(subject, part)

    async def _send_media(self, subject: ChannelSubject, media: ContentPart) -> None:
        wa_id = self._recipient(subject)
        if not wa_id:
            return
        label = self._get_media_label(media)
        try:
            data, mime = await self.load_media_bytes(media)
            limit = self._media_limit(media)
            if len(data) > limit:
                await self._send_text(subject, f"[{label}: file too large]")
                return
            http = await self._ensure_http()
            filename = getattr(media, "filename", None) or f"file{(mime or '').split('/')[-1]}"
            media_id = await upload_media(
                http,
                self._config.effective_graph_url(),
                self._config.phone_number_id,
                self._config.access_token,
                data,
                mime,
                filename,
            )
            kind = self._media_kind(media)
            payload: dict[str, Any] = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": wa_id,
                "type": kind,
                kind: {"id": media_id},
            }
            if kind == "document":
                payload["document"]["filename"] = filename
            await send_message(
                http,
                self._config.effective_graph_url(),
                self._config.phone_number_id,
                self._config.access_token,
                payload,
            )
        except (WhatsAppApiError, ValueError, RuntimeError, aiohttp.ClientError):
            logger.exception("WhatsApp send_media failed")
            await self._send_text(subject, f"[{label} (upload failed)]")

    # ------------------------------------------------------------------
    # Proactive push — enforce the 24h free-form service window
    # ------------------------------------------------------------------

    def _outside_service_window(self, subject: ChannelSubject) -> bool:
        known = self._known_subjects.get(subject.subject_id)
        if known is None:
            return False
        last_seen = known.last_seen or 0
        if last_seen <= 0:
            return False
        return (time.time() - last_seen) > _SERVICE_WINDOW_SECONDS

    async def push_message(self, subject: ChannelSubject, parts: list[ContentPart]) -> None:
        if self._outside_service_window(subject):
            logger.warning(
                "WhatsApp push dropped: 24h service window expired for subject %s",
                subject.subject_id,
            )
            return
        await super().push_message(subject, parts)

    async def push_text(self, subject: ChannelSubject, text: str) -> None:
        if self._outside_service_window(subject):
            logger.warning(
                "WhatsApp push_text dropped: 24h service window expired for subject %s",
                subject.subject_id,
            )
            return
        await super().push_text(subject, text)

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    @staticmethod
    def _media_part_for(msg_type: str, body: dict[str, Any]) -> ContentPart | None:
        media_id = str(body.get("id") or "")
        if not media_id:
            return None
        mime = str(body.get("mime_type") or "") or None
        url = f"{_MEDIA_URL_PREFIX}{media_id}"
        if msg_type == "image":
            return ImageContent(url=url, mime_type=mime)
        if msg_type == "video":
            return VideoContent(url=url, mime_type=mime)
        if msg_type == "audio":
            return AudioContent(url=url, mime_type=mime)
        if msg_type == "sticker":
            return ImageContent(url=url, mime_type=mime or "image/webp")
        if msg_type == "document":
            return FileContent(
                url=url,
                filename=str(body.get("filename") or "attachment"),
                mime_type=mime,
            )
        return None

    @classmethod
    def _parse_one_message(
        cls,
        message: dict[str, Any],
        contact: dict[str, Any] | None,
        phone_number_id: str,
        display_phone_number: str,
    ) -> InboundMessage | None:
        wa_id = str(message.get("from") or "")
        msg_type = str(message.get("type") or "")
        if not wa_id or not msg_type:
            return None

        parts: list[ContentPart] = []
        if msg_type == "text":
            body = str((message.get("text") or {}).get("body") or "")
            if body:
                parts.append(TextContent(text=body))
        elif msg_type == "reaction":
            # Emoji reactions carry no conversational payload; skip.
            return None
        elif msg_type in {"image", "video", "audio", "sticker", "document"}:
            part = cls._media_part_for(msg_type, message.get(msg_type) or {})
            if part is not None:
                parts.append(part)
        elif msg_type in {"location", "contacts", "unsupported", "button", "interactive"}:
            text = f"[Unsupported WhatsApp message type: {msg_type}]"
            parts.append(TextContent(text=text))
        else:
            text = f"[Unknown WhatsApp message type: {msg_type}]"
            parts.append(TextContent(text=text))

        if not parts:
            return None

        display_name = ""
        if isinstance(contact, dict):
            display_name = str((contact.get("profile") or {}).get("name") or "")

        metadata = {
            "wa_id": wa_id,
            "wa_message_id": str(message.get("id") or ""),
            "phone_number_id": phone_number_id,
            "display_phone_number": display_phone_number,
            "wa_type": msg_type,
        }
        try:
            timestamp = float(message.get("timestamp") or 0) or None
        except (TypeError, ValueError):
            timestamp = None

        kwargs: dict[str, Any] = {}
        if timestamp:
            kwargs["timestamp"] = timestamp
        return InboundMessage(
            channel_id="",
            channel_type=WHATSAPP_CHANNEL_TYPE,
            channel_subject=ChannelSubject(
                subject_id=wa_id,
                display_name=display_name,
                chat_type="direct",
                metadata=metadata,
            ),
            content=parts,
            metadata=metadata,
            **kwargs,
        )

    @classmethod
    def _iter_raw_messages(cls, raw_payload: Any) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
        """Yield ``(message, contact)`` pairs from a webhook payload.

        Accepts both the route-split shape (``{"value", "message", "contact"}``)
        and a full Meta webhook body (first message wins) for robustness.
        """
        if not isinstance(raw_payload, dict):
            return []
        if isinstance(raw_payload.get("message"), dict):
            # Route-split shape produced by the webhook callback route.
            contact = raw_payload.get("contact")
            return [(raw_payload["message"], contact if isinstance(contact, dict) else None)]
        if raw_payload.get("object") == "whatsapp_business_account":
            pairs: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
            for entry in raw_payload.get("entry") or []:
                if not isinstance(entry, dict):
                    continue
                for change in entry.get("changes") or []:
                    if not isinstance(change, dict):
                        continue
                    value = change.get("value") or {}
                    if not isinstance(value, dict):
                        continue
                    contacts = value.get("contacts") or []
                    contact_map = {
                        c.get("wa_id"): c for c in contacts if isinstance(c, dict)
                    }
                    for message in value.get("messages") or []:
                        if not isinstance(message, dict):
                            continue
                        pairs.append((message, contact_map.get(message.get("from"))))
            return pairs
        return []

    # Upstream BaseChannel annotates `-> InboundMessage`, but the manager worker
    # itself handles None ("if message is not None"), so None-on-unparseable is
    # the de-facto contract; the annotation there is too narrow.
    def parse_inbound(self, raw_payload: Any) -> InboundMessage | None:  # type: ignore[override]
        if isinstance(raw_payload, InboundMessage):
            if not raw_payload.channel_id:
                raw_payload.channel_id = self.channel_id
                raw_payload.tenant_id = self._tenant_id
            return raw_payload

        pairs = self._iter_raw_messages(raw_payload)
        if not pairs:
            return None
        message, contact = pairs[0]
        value = raw_payload.get("value") if isinstance(raw_payload, dict) else None
        metadata = (value or {}).get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        parsed = self._parse_one_message(
            message,
            contact,
            str(metadata.get("phone_number_id") or self._config.phone_number_id),
            str(metadata.get("display_phone_number") or ""),
        )
        if parsed is not None:
            parsed.channel_id = self.channel_id
            parsed.tenant_id = self._tenant_id
            parsed.channel_session_id = self._connection_session or f"{self.channel_id}-unconnected"
        return parsed

    async def _preprocess_inbound(self, message: InboundMessage) -> None:
        """Resolve ``wa-media://<id>`` markers to real download URLs."""
        for part in message.content:
            url = getattr(part, "url", "")
            if not url.startswith(_MEDIA_URL_PREFIX):
                continue
            if not isinstance(part, (ImageContent, VideoContent, AudioContent, FileContent)):
                continue
            media_id = url[len(_MEDIA_URL_PREFIX):]
            try:
                http = await self._ensure_http()
                download_url, mime = await resolve_media_url(
                    http,
                    self._config.effective_graph_url(),
                    media_id,
                    self._config.access_token,
                )
                part.url = download_url
                if mime and not part.mime_type:
                    part.mime_type = mime
            except Exception:  # pylint: disable=broad-except
                logger.exception("WhatsApp media resolution failed (id=%s)", media_id)

    async def fetch_remote_media(self, url: str) -> tuple[bytes, str]:
        """Download media, attaching the Bearer token for Meta-owned hosts."""
        host = urlparse(url).netloc.lower()
        http = await self._ensure_http()
        if host in MEDIA_DOWNLOAD_HOSTS:
            headers = {"Authorization": f"Bearer {self._config.access_token}"}
            async with http.get(url, headers=headers) as resp:
                resp.raise_for_status()
                content_type = resp.content_type or "application/octet-stream"
                return await resp.read(), content_type
        return await super().fetch_remote_media(url)
