"""Sendblue iMessage platform adapter.

Connects to the Sendblue REST API (https://api.sendblue.co) for outbound
iMessage/SMS/RCS and runs an aiohttp webhook server to receive inbound messages.
Sendblue handles the iMessage -> RCS -> SMS fallback cascade automatically.

Required env vars:
  - SENDBLUE_API_KEY_ID       (sb-api-key-id header)
  - SENDBLUE_API_SECRET_KEY   (sb-api-secret-key header)
  - SENDBLUE_NUMBER           (E.164 from-number, e.g. +13472551483)

Gateway-specific env vars:
  - SENDBLUE_WEBHOOK_HOST      (default 0.0.0.0)
  - SENDBLUE_WEBHOOK_PORT      (default 8646; set to ${PORT} on Railway)
  - SENDBLUE_WEBHOOK_PATH      (default /sendblue-webhook)
  - SENDBLUE_WEBHOOK_TOKEN     (optional shared secret; required as ?token= when set)
  - SENDBLUE_ALLOWED_USERS     (comma-separated E.164 numbers)
  - SENDBLUE_ALLOW_ALL_USERS   (true/false)
  - SENDBLUE_HOME_CHANNEL      (number for cron delivery)

Sendblue does not sign inbound webhooks, so authenticity rests on the optional
shared-secret token plus the gateway's per-user allowlist.
"""

import asyncio
import hmac
import json
import logging
import os
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.platforms.helpers import redact_phone, strip_markdown

logger = logging.getLogger(__name__)

SENDBLUE_API_BASE = "https://api.sendblue.co"
SEND_MESSAGE_PATH = "/api/send-message"
TYPING_PATH = "/api/send-typing-indicator"
MAX_SENDBLUE_LENGTH = 4000  # well under Sendblue's 18996 cap; nicer iMessage bubbles
DEFAULT_WEBHOOK_HOST = "0.0.0.0"  # cloud-first (Railway); set 127.0.0.1 + tunnel locally
DEFAULT_WEBHOOK_PORT = 8646
DEFAULT_WEBHOOK_PATH = "/sendblue-webhook"
WEBHOOK_BODY_MAX_BYTES = 65_536  # iMessage payloads are tiny; 64 KiB is generous
_DEDUP_MAX = 500  # bounded LRU of recent message handles (Sendblue redelivers)
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


def _extra_or_env(extra: Dict[str, Any], key: str, env: str, default: str = "") -> str:
    """Read a setting from PlatformConfig.extra, falling back to an env var."""
    value = extra.get(key)
    if value is None or value == "":
        value = os.getenv(env, default)
    return str(value).strip()


def check_requirements() -> bool:
    """Plugin gate: require credentials AND aiohttp at runtime."""
    if not os.getenv("SENDBLUE_API_KEY_ID"):
        return False
    if not os.getenv("SENDBLUE_API_SECRET_KEY"):
        return False
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


class SendblueAdapter(BasePlatformAdapter):
    """Sendblue iMessage <-> Hermes gateway adapter.

    Each inbound phone number gets its own Hermes session (multi-tenant).
    Replies are always sent from the configured SENDBLUE_NUMBER.
    """

    MAX_MESSAGE_LENGTH = MAX_SENDBLUE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("sendblue"))
        extra = getattr(config, "extra", {}) or {}

        self._api_key_id = _extra_or_env(extra, "api_key_id", "SENDBLUE_API_KEY_ID")
        self._api_secret_key = _extra_or_env(
            extra, "api_secret_key", "SENDBLUE_API_SECRET_KEY"
        )
        self._from_number = _extra_or_env(extra, "from_number", "SENDBLUE_NUMBER")

        self._webhook_host = _extra_or_env(
            extra, "webhook_host", "SENDBLUE_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST
        )
        self._webhook_port = int(
            _extra_or_env(
                extra, "webhook_port", "SENDBLUE_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT)
            )
        )
        self._webhook_path = _extra_or_env(
            extra, "webhook_path", "SENDBLUE_WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH
        )
        if not self._webhook_path.startswith("/"):
            self._webhook_path = f"/{self._webhook_path}"
        self._webhook_token = _extra_or_env(
            extra, "webhook_token", "SENDBLUE_WEBHOOK_TOKEN"
        )

        self._runner = None
        self._http_session: Optional["aiohttp.ClientSession"] = None
        self._seen_ids: "OrderedDict[str, None]" = OrderedDict()

    def _headers(self) -> Dict[str, str]:
        """Sendblue auth headers."""
        return {
            "sb-api-key-id": self._api_key_id,
            "sb-api-secret-key": self._api_secret_key,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Required abstract methods
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        import aiohttp
        from aiohttp import web

        if not self._api_key_id or not self._api_secret_key:
            msg = "[sendblue] SENDBLUE_API_KEY_ID / SENDBLUE_API_SECRET_KEY not set"
            logger.error(msg)
            self._set_fatal_error("sendblue_missing_credentials", msg, retryable=False)
            return False

        if not self._from_number:
            msg = "[sendblue] SENDBLUE_NUMBER not set — cannot send replies"
            logger.error(msg)
            self._set_fatal_error("sendblue_missing_from_number", msg, retryable=False)
            return False

        # Sendblue does not sign webhooks, so the shared-secret token is the only
        # transport-layer gate. Refuse to start without it unless explicitly opted out.
        insecure_no_token = (
            os.getenv("SENDBLUE_INSECURE_NO_TOKEN", "").lower() == "true"
        )
        if not self._webhook_token and not insecure_no_token:
            msg = (
                "[sendblue] Refusing to start: SENDBLUE_WEBHOOK_TOKEN is required "
                "(Sendblue does not sign requests). Set it and register the webhook "
                "URL as .../sendblue-webhook?token=<value>. For local dev without a "
                "token, set SENDBLUE_INSECURE_NO_TOKEN=true (NOT for production)."
            )
            logger.error(msg)
            self._set_fatal_error("sendblue_missing_webhook_token", msg, retryable=False)
            return False
        if not self._webhook_token:
            logger.warning(
                "[sendblue] SENDBLUE_INSECURE_NO_TOKEN=true — webhook is "
                "unauthenticated. Anyone who reaches it can inject messages. The "
                "per-user allowlist still applies. Do NOT use this in production."
            )

        app = web.Application(client_max_size=WEBHOOK_BODY_MAX_BYTES)
        app.router.add_post(self._webhook_path, self._handle_webhook)
        app.router.add_get("/health", lambda _: web.Response(text="ok"))

        try:
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, self._webhook_host, self._webhook_port)
            await site.start()
        except OSError as exc:
            msg = (
                f"[sendblue] Could not bind webhook on "
                f"{self._webhook_host}:{self._webhook_port}: {exc}"
            )
            logger.error(msg)
            self._set_fatal_error("sendblue_bind_failed", msg, retryable=True)
            return False

        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        self._mark_connected()

        logger.info(
            "[sendblue] webhook listening on %s:%d%s, from: %s",
            self._webhook_host,
            self._webhook_port,
            self._webhook_path,
            redact_phone(self._from_number),
        )
        return True

    async def disconnect(self) -> None:
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._running = False
        logger.info("[sendblue] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted)
        last_result = SendResult(success=True)
        for chunk in chunks:
            result = await self._post_message(chat_id, chunk)
            if not result.success:
                return result
            last_result = result
        return last_result

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Sendblue accepts a remote media_url directly."""
        return await self._post_message(
            chat_id, self.format_message(caption or ""), media_url=image_url
        )

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Best-effort iMessage typing bubble; never blocks or raises."""
        payload = {"number": chat_id, "from_number": self._from_number}
        try:
            session = self._http_session
            if session is None:
                return
            async with session.post(
                f"{SENDBLUE_API_BASE}{TYPING_PATH}",
                json=payload,
                headers=self._headers(),
            ) as resp:
                if resp.status >= 400:
                    logger.debug(
                        "[sendblue] typing indicator to %s failed: %s",
                        redact_phone(chat_id),
                        resp.status,
                    )
        except Exception as exc:  # noqa: BLE001 — typing is non-critical
            logger.debug("[sendblue] typing indicator error: %s", exc)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    def format_message(self, content: str) -> str:
        """Strip markdown — iMessage renders it as literal characters."""
        return strip_markdown(content)

    # ------------------------------------------------------------------
    # Outbound HTTP
    # ------------------------------------------------------------------

    async def _post_message(
        self, chat_id: str, content: str, media_url: Optional[str] = None
    ) -> SendResult:
        import aiohttp

        payload: Dict[str, Any] = {
            "number": chat_id,
            "from_number": self._from_number,
        }
        if content:
            payload["content"] = content
        if media_url:
            payload["media_url"] = media_url

        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        try:
            async with session.post(
                f"{SENDBLUE_API_BASE}{SEND_MESSAGE_PATH}",
                json=payload,
                headers=self._headers(),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    error_msg = ""
                    if isinstance(body, dict):
                        error_msg = body.get("message") or body.get("error_message") or ""
                    error_msg = error_msg or f"HTTP {resp.status}"
                    logger.error(
                        "[sendblue] send failed to %s: %s %s",
                        redact_phone(chat_id),
                        resp.status,
                        error_msg,
                    )
                    return SendResult(
                        success=False,
                        error=f"Sendblue {resp.status}: {error_msg}",
                        error_kind=_error_kind(resp.status),
                        retryable=resp.status == 429 or resp.status >= 500,
                    )
                msg_id = ""
                if isinstance(body, dict):
                    msg_id = body.get("message_handle") or body.get("handle") or ""
                return SendResult(success=True, message_id=str(msg_id))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[sendblue] send error to %s: %s", redact_phone(chat_id), exc
            )
            return SendResult(success=False, error=str(exc), retryable=True)
        finally:
            if not self._http_session and session:
                await session.close()

    # ------------------------------------------------------------------
    # Inbound webhook handler
    # ------------------------------------------------------------------

    async def _handle_webhook(self, request) -> "aiohttp.web.Response":
        from aiohttp import web

        # Shared-secret gate (Sendblue does not sign requests) — constant-time.
        if self._webhook_token:
            supplied = request.query.get("token") or request.headers.get(
                "X-Webhook-Token", ""
            )
            if not hmac.compare_digest(
                str(supplied).encode("utf-8"), self._webhook_token.encode("utf-8")
            ):
                logger.warning("[sendblue] Rejected webhook: bad or missing token")
                return web.json_response({"error": "unauthorized"}, status=401)

        try:
            raw = await request.read()
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001
            logger.error("[sendblue] webhook parse error: %s", exc)
            return web.json_response({"error": "invalid payload"}, status=400)

        if not isinstance(payload, dict):
            return web.json_response({"ok": True})

        from_number = str(payload.get("from_number", "")).strip()
        content = str(payload.get("content", "") or "").strip()
        message_id = str(payload.get("message_handle", "") or "")

        # Ignore echoes of our own number and non-inbound events.
        if payload.get("is_outbound"):
            return web.json_response({"ok": True})
        if from_number and from_number == self._from_number:
            logger.debug("[sendblue] ignoring echo from own number")
            return web.json_response({"ok": True})
        if not from_number or not content:
            return web.json_response({"ok": True})

        # Reject spoofed/malformed sender identities before they become a session key.
        if not _E164_RE.match(from_number):
            logger.warning("[sendblue] ignoring webhook with invalid from_number")
            return web.json_response({"ok": True})

        # Drop redelivered messages (Sendblue retries) to avoid double agent runs.
        if message_id:
            if message_id in self._seen_ids:
                logger.debug("[sendblue] ignoring duplicate message %s", message_id)
                return web.json_response({"ok": True})
            self._seen_ids[message_id] = None
            while len(self._seen_ids) > _DEDUP_MAX:
                self._seen_ids.popitem(last=False)

        safe_content = content[:80].replace("\n", "\\n").replace("\r", "\\r")
        logger.info(
            "[sendblue] inbound from %s: %s",
            redact_phone(from_number),
            safe_content,
        )

        source = self.build_source(
            chat_id=from_number,
            chat_name=from_number,
            chat_type="dm",
            user_id=from_number,
            user_name=from_number,
        )
        event = MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=message_id,
        )

        # Non-blocking: Sendblue only needs a fast 2xx ack.
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return web.json_response({"ok": True})


def _error_kind(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "forbidden"
    if status == 404:
        return "not_found"
    if status >= 500:
        return "transient"
    return "unknown"


# ──────────────────────────────────────────────────────────────────────────
# Plugin registration glue
# ──────────────────────────────────────────────────────────────────────────


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    has_key = bool(os.getenv("SENDBLUE_API_KEY_ID") or extra.get("api_key_id"))
    has_secret = bool(
        os.getenv("SENDBLUE_API_SECRET_KEY") or extra.get("api_secret_key")
    )
    return has_key and has_secret


def is_connected(config) -> bool:
    """Surface in ``hermes status`` even before the adapter is instantiated."""
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    """Auto-seed PlatformConfig.extra from an env-only setup (no config.yaml block)."""
    if not (
        os.getenv("SENDBLUE_API_KEY_ID") and os.getenv("SENDBLUE_API_SECRET_KEY")
    ):
        return None
    seeded: Dict[str, Any] = {
        "api_key_id": os.environ["SENDBLUE_API_KEY_ID"],
        "api_secret_key": os.environ["SENDBLUE_API_SECRET_KEY"],
    }
    if os.getenv("SENDBLUE_NUMBER"):
        seeded["from_number"] = os.environ["SENDBLUE_NUMBER"]
    if os.getenv("SENDBLUE_WEBHOOK_HOST"):
        seeded["webhook_host"] = os.environ["SENDBLUE_WEBHOOK_HOST"]
    if os.getenv("SENDBLUE_WEBHOOK_PORT"):
        seeded["webhook_port"] = os.environ["SENDBLUE_WEBHOOK_PORT"]
    if os.getenv("SENDBLUE_WEBHOOK_PATH"):
        seeded["webhook_path"] = os.environ["SENDBLUE_WEBHOOK_PATH"]
    if os.getenv("SENDBLUE_WEBHOOK_TOKEN"):
        seeded["webhook_token"] = os.environ["SENDBLUE_WEBHOOK_TOKEN"]
    if os.getenv("SENDBLUE_HOME_CHANNEL"):
        seeded["home_channel"] = os.environ["SENDBLUE_HOME_CHANNEL"]
    return seeded


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process delivery for cron jobs detached from the gateway.

    ``thread_id``/``media_files``/``force_document`` are accepted for signature
    parity; Sendblue is 1:1 by number and cron-side media needs a public URL we
    can't construct here, so we send text only.
    """
    try:
        import aiohttp
    except ImportError:
        return {"error": "aiohttp not installed. Run: pip install aiohttp"}

    extra = getattr(pconfig, "extra", {}) or {}
    api_key_id = os.getenv("SENDBLUE_API_KEY_ID") or extra.get("api_key_id", "")
    api_secret_key = os.getenv("SENDBLUE_API_SECRET_KEY") or extra.get(
        "api_secret_key", ""
    )
    from_number = os.getenv("SENDBLUE_NUMBER") or extra.get("from_number", "")
    if not api_key_id or not api_secret_key or not from_number:
        return {
            "error": (
                "Sendblue not configured (SENDBLUE_API_KEY_ID, "
                "SENDBLUE_API_SECRET_KEY, SENDBLUE_NUMBER required)"
            )
        }

    message = strip_markdown(message)
    headers = {
        "sb-api-key-id": api_key_id,
        "sb-api-secret-key": api_secret_key,
        "Content-Type": "application/json",
    }
    payload = {"number": chat_id, "from_number": from_number, "content": message}
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), trust_env=True
        ) as session:
            async with session.post(
                f"{SENDBLUE_API_BASE}{SEND_MESSAGE_PATH}",
                json=payload,
                headers=headers,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400:
                    error_msg = ""
                    if isinstance(body, dict):
                        error_msg = body.get("message") or body.get("error_message") or ""
                    return {"error": f"Sendblue API error ({resp.status}): {error_msg}"}
                msg_id = body.get("message_handle", "") if isinstance(body, dict) else ""
                return {
                    "success": True,
                    "platform": "sendblue",
                    "chat_id": chat_id,
                    "message_id": msg_id,
                }
    except Exception as exc:  # noqa: BLE001 — avoid leaking phone/URL from exc text
        return {"error": f"Sendblue send failed: {type(exc).__name__}"}


def _build_adapter(config):
    """Factory wrapper that constructs SendblueAdapter from a PlatformConfig."""
    return SendblueAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="sendblue",
        label="Sendblue (iMessage)",
        adapter_factory=_build_adapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[
            "SENDBLUE_API_KEY_ID",
            "SENDBLUE_API_SECRET_KEY",
            "SENDBLUE_NUMBER",
        ],
        install_hint="pip install aiohttp",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="SENDBLUE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="SENDBLUE_ALLOWED_USERS",
        allow_all_env="SENDBLUE_ALLOW_ALL_USERS",
        max_message_length=MAX_SENDBLUE_LENGTH,
        emoji="💬",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via iMessage (Sendblue). iMessage does NOT render "
            "Markdown — asterisks and hashes show up literally, so use plain text. "
            "Keep replies concise and conversational, like real text messages; long "
            "answers are split into multiple bubbles. Sendblue auto-falls back "
            "iMessage -> RCS -> SMS, so avoid relying on iMessage-only rich features. "
            "You can attach media by returning MEDIA:/absolute/path/to/file."
        ),
    )
