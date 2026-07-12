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
  - SENDBLUE_USER_NAMES        (optional "+E164=Name,..." roster; display only)
  - SENDBLUE_HOME_CHANNEL      (number for cron delivery)

Onboard/offboard a teammate (keeps ALLOWED_USERS + USER_NAMES in sync) with
scripts/add_teammate.py — see deploy/TEAMMATES.md.

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
    cache_audio_from_url,
    cache_image_from_url,
)
from gateway.platforms.helpers import redact_phone, strip_markdown

logger = logging.getLogger(__name__)

SENDBLUE_API_BASE = "https://api.sendblue.co"
SEND_MESSAGE_PATH = "/api/send-message"
TYPING_PATH = "/api/send-typing-indicator"
UPLOAD_FILE_PATH = "/api/upload-file"
MAX_SENDBLUE_LENGTH = 4000  # well under Sendblue's 18996 cap; nicer iMessage bubbles
DEFAULT_WEBHOOK_HOST = "0.0.0.0"  # cloud-first (Railway); set 127.0.0.1 + tunnel locally
DEFAULT_WEBHOOK_PORT = 8646
DEFAULT_WEBHOOK_PATH = "/sendblue-webhook"
WEBHOOK_BODY_MAX_BYTES = 65_536  # iMessage payloads are tiny; 64 KiB is generous
_DEDUP_MAX = 500  # bounded LRU of recent message handles (Sendblue redelivers)
_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

# Max bubbles per send() call -- prevents cron runaway fan-out on long output.
_MAX_BUBBLES = 5
# Min fragment length; shorter fragments are merged into the previous bubble.
_MIN_FRAGMENT_LEN = 25
# Split threshold: paragraph-split only below this total length.
_PARAGRAPH_SPLIT_THRESHOLD = 2000


# ---------------------------------------------------------------------------
# Humanizer: rewrite gateway boilerplate into texting-voice strings.
# Ordered list of (pattern, replacement) tuples; first match wins.
# Pass-through (fail-open): if no pattern matches, return text unchanged.
# Replacement is a string or callable(match) -> str.
# ---------------------------------------------------------------------------

# Compiled once at module load for performance.
_HUMANIZE_TABLE: List[tuple] = [
    # run.py:357-360 -- provider auth failure
    (
        re.compile(r"^\s*⚠️\s+Provider authentication failed", re.IGNORECASE),
        "something's broken on my end (auth issue), give me a bit",
    ),
    # run.py:361-365 -- provider policy rejection
    (
        re.compile(r"^\s*⚠️\s+The model provider rejected", re.IGNORECASE),
        "hm, that one got blocked on my end. try saying it differently?",
    ),
    # run.py:367 -- rate limiting
    (
        re.compile(r"^\s*⏱️\s+The model provider is rate.limit", re.IGNORECASE),
        "i'm getting rate limited, give me a minute and try again",
    ),
    # run.py:369-371 -- provider failed after retries
    (
        re.compile(r"^\s*⚠️\s+The model provider failed after retries", re.IGNORECASE),
        "something's broken on my end, give me a min and resend that",
    ),
    # run.py:2437-2440 -- context window too large
    (
        re.compile(r"^\s*⚠️\s+Session too large", re.IGNORECASE),
        "my head's too full from this convo, text /reset and we'll start fresh",
    ),
    # run.py:2442-2444 -- request failed with raw error detail. Anchored on the
    # gateway's trailing "/reset" hint too, so agent prose that merely starts
    # with "The request failed:" passes through untouched.
    (
        re.compile(r"^\s*The request failed:.*/reset", re.IGNORECASE | re.DOTALL),
        "that didn't go through on my end. try again in a sec",
    ),
    # run.py:2450 -- processing stopped partial
    (
        re.compile(r"^\s*⚠️\s+Processing stopped:", re.IGNORECASE),
        "missed that one while i was wrapping something up, send it again?",
    ),
    # run.py:2451-2454 -- processing completed but no response
    (
        re.compile(r"^\s*⚠️\s+Processing completed but no response", re.IGNORECASE),
        "missed that one while i was wrapping something up, send it again?",
    ),
    # run.py:2467-2470 -- message wasn't processed (previous turn cleaned up)
    (
        re.compile(r"^\s*⚠️\s+Your message wasn't processed", re.IGNORECASE),
        "missed that one while i was wrapping something up, send it again?",
    ),
    # run.py:10319-10323 -- session automatically reset (suspended-bypass survivor)
    (
        re.compile(r"^\s*◐\s+Session automatically reset", re.IGNORECASE),
        "heads up, i had to restart so we're starting fresh. what were we on?",
    ),
    # run.py:8494-8499 -- pairing message with code (regex extracts code)
    (
        re.compile(
            r"Hi~\s+I don't recognize you yet.*?`hermes pairing approve \S+ (\S+)`",
            re.DOTALL | re.IGNORECASE,
        ),
        lambda m: f"hey! i don't know this number yet. ask Jihan to add you, your code is {m.group(1)}",
    ),
    # run.py:8504-8506 -- too many pairing requests
    (
        re.compile(r"Too many pairing requests right now", re.IGNORECASE),
        "too many pairing requests right now, try again in a bit",
    ),
    # run.py:18120 -- heartbeat "Working" bubble (gateway string has an em dash
    # after "Working"; anchor without the dash so punctuation drift can't break it)
    (
        re.compile(r"^\s*⏳\s+Working\b", re.IGNORECASE),
        "one sec, still on your last thing",
    ),
    # run.py:5168 -- subagent working queued
    (
        re.compile(r"^\s*⏳\s+Subagent working", re.IGNORECASE),
        "one sec, still on your last thing",
    ),
    # run.py:5173 -- compressing context queued
    (
        re.compile(r"^\s*⏳\s+Compressing context", re.IGNORECASE),
        "one sec, still on your last thing",
    ),
    # run.py:5178 -- queued for next turn
    (
        re.compile(r"^\s*⏳\s+Queued for the next turn", re.IGNORECASE),
        "one sec, still on your last thing",
    ),
    # run.py:5183 -- interrupting current task
    (
        re.compile(r"^\s*⚡\s+Interrupting current task", re.IGNORECASE),
        "one sec, still on your last thing",
    ),
    # scheduler.py:69 -- cron heartbeat job failure: suppress entirely (return "")
    (
        re.compile(r"^\s*⚠️\s+Cron '(heartbeat[^']*)'", re.IGNORECASE),
        "",
    ),
    # scheduler.py:69-100 -- other cron failure: human note
    (
        re.compile(r"^\s*⚠️\s+Cron '([^']+)' failed:", re.IGNORECASE),
        lambda m: f"hm, my reminder for \"{m.group(1)}\" hit a snag, might be worth checking",
    ),
]


# delivery.py:445 -- truncated cron output footer. Substituted in place (the
# real content precedes the footer, so this must NOT replace the whole message).
# Dead branch for sendblue after splits_long_messages=True; kept defensively.
_RE_TRUNC_FOOTER = re.compile(
    r"\n*\.\.\.\s*\[truncated, full output saved to [^\]]*\]", re.IGNORECASE
)
_CUT_SHORT_NOTE = "(cut it short, ask me if you want the rest)"


def _humanize_gateway_notice(text: str) -> str:
    """Rewrite known gateway boilerplate into texting-voice strings.

    Iterates the ordered _HUMANIZE_TABLE; first match wins. Fail-open:
    unrecognized text is returned unchanged. Called before strip_markdown
    in format_message() and also in _standalone_send().
    """
    # In-place footer rewrite first: keeps the content, humanizes the suffix.
    text = _RE_TRUNC_FOOTER.sub(f"\n\n{_CUT_SHORT_NOTE}", text)
    for pattern, replacement in _HUMANIZE_TABLE:
        m = pattern.search(text)
        if m:
            if callable(replacement):
                return replacement(m)
            return replacement
    return text


# ---------------------------------------------------------------------------
# Extended markdown stripper: runs AFTER strip_markdown() in format_message().
# Does NOT duplicate work already done by helpers.strip_markdown().
# ---------------------------------------------------------------------------

_RE_BULLET = re.compile(r"^[-*+]\s+", re.MULTILINE)
_RE_BLOCKQUOTE = re.compile(r"^>\s?", re.MULTILINE)
_RE_HR = re.compile(r"^([-*_]\s?){3,}$", re.MULTILINE)
_RE_STRIKETHROUGH = re.compile(r"~~(.+?)~~", re.DOTALL)
_RE_EM_DASH_SPACED = re.compile(r" — ")
_RE_EM_DASH_BARE = re.compile(r"—")
# Guard: leading [Name] tag echo at message start only (1-24 non-newline chars)
_RE_NAME_TAG_LEAD = re.compile(r"^\[[^\]\n]{1,24}\]\s+")
# GFM pipe table: header row + separator row + data rows
_RE_TABLE_SEPARATOR = re.compile(r"^\|[-| :]+\|$")


def _strip_markdown_extra(text: str) -> str:
    """Strip remaining markdown artifacts not handled by helpers.strip_markdown().

    Handles: bullets, blockquotes, horizontal rules, strikethrough, GFM pipe
    tables, em-dash normalization, and leading [Name] tag echo guard.
    """
    # GFM pipe tables: convert to col: val lines before splitting on newlines.
    lines = text.split("\n")
    result_lines: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect a table header row (starts and ends with |)
        if line.startswith("|") and line.endswith("|") and i + 1 < len(lines):
            next_line = lines[i + 1]
            if _RE_TABLE_SEPARATOR.match(next_line.strip()):
                # Parse headers
                headers = [h.strip() for h in line.strip("|").split("|")]
                i += 2  # skip header + separator
                # Consume data rows
                while i < len(lines) and lines[i].startswith("|") and lines[i].endswith("|"):
                    cells = [c.strip() for c in lines[i].strip("|").split("|")]
                    for col_idx, cell in enumerate(cells):
                        col_name = headers[col_idx] if col_idx < len(headers) else f"col{col_idx}"
                        result_lines.append(f"{col_name}: {cell}")
                    i += 1
                continue
        result_lines.append(line)
        i += 1
    text = "\n".join(result_lines)

    # Bullet markers: drop the marker, keep the content on its own line.
    text = _RE_BULLET.sub("", text)
    # Blockquote markers: drop the leading "> " or ">"
    text = _RE_BLOCKQUOTE.sub("", text)
    # Horizontal rules: drop the whole line
    text = _RE_HR.sub("", text)
    # Strikethrough: unwrap
    text = _RE_STRIKETHROUGH.sub(r"\1", text)
    # Em-dash: spaced variant -> ", "; bare -> "-"
    text = _RE_EM_DASH_SPACED.sub(", ", text)
    text = _RE_EM_DASH_BARE.sub("-", text)
    # Leading [Name] tag echo guard (message start only)
    text = _RE_NAME_TAG_LEAD.sub("", text)

    return text


def _extra_or_env(extra: Dict[str, Any], key: str, env: str, default: str = "") -> str:
    """Read a setting from PlatformConfig.extra, falling back to an env var."""
    value = extra.get(key)
    if value is None or value == "":
        value = os.getenv(env, default)
    return str(value).strip()


def _parse_user_names(raw: str) -> Dict[str, str]:
    """Parse a roster string into an E.164 -> display-name map.

    Format: comma-separated ``+E164=Name`` pairs, e.g.
    ``+14843693839=Jihan,+14259853177=Kunal``. Lets a multi-user deployment
    address each sender by name (and lets the agent see who a message is from)
    instead of a raw phone number. Malformed entries are skipped.
    """
    mapping: Dict[str, str] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        number, name = pair.split("=", 1)
        number, name = number.strip(), name.strip()
        if number and name:
            mapping[number] = name
    return mapping


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
    # Signals delivery.py to skip its truncate-with-path-footer branch; this
    # adapter chunks natively via send() paragraph splitting + truncate_message.
    splits_long_messages = True

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
        # Port precedence: explicit config/env -> Railway's injected $PORT -> default.
        # Binding $PORT lets Railway (and similar PaaS) route + health-check the
        # webhook automatically without a hardcoded port.
        self._webhook_port = int(
            _extra_or_env(extra, "webhook_port", "SENDBLUE_WEBHOOK_PORT", "")
            or os.getenv("PORT", "")
            or str(DEFAULT_WEBHOOK_PORT)
        )
        self._webhook_path = _extra_or_env(
            extra, "webhook_path", "SENDBLUE_WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH
        )
        if not self._webhook_path.startswith("/"):
            self._webhook_path = f"/{self._webhook_path}"
        self._webhook_token = _extra_or_env(
            extra, "webhook_token", "SENDBLUE_WEBHOOK_TOKEN"
        )
        # Optional roster mapping sender E.164 numbers to display names so the
        # agent always knows who it is texting on a shared number (multi-user
        # deployments). Empty roster => fall back to the raw number.
        self._user_names = _parse_user_names(
            _extra_or_env(extra, "user_names", "SENDBLUE_USER_NAMES", "")
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
            msg = "[sendblue] SENDBLUE_NUMBER not set -- cannot send replies"
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
                "[sendblue] SENDBLUE_INSECURE_NO_TOKEN=true -- webhook is "
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
        """Format, split into natural bubbles, and send sequentially."""
        formatted = self.format_message(content)

        # Empty after formatting: skip API call, return success.
        if not formatted.strip():
            return SendResult(success=True)

        if len(formatted) < _PARAGRAPH_SPLIT_THRESHOLD:
            # Paragraph-split path: split on double newline, merge short fragments.
            raw_fragments = formatted.split("\n\n")
            bubbles: List[str] = []
            for frag in raw_fragments:
                frag = frag.strip()
                if not frag:
                    continue
                if bubbles and len(frag) < _MIN_FRAGMENT_LEN:
                    # Merge short fragment into the previous bubble.
                    bubbles[-1] = bubbles[-1] + "\n\n" + frag
                else:
                    bubbles.append(frag)
            # Cap at _MAX_BUBBLES; join overflow into the last bubble.
            if len(bubbles) > _MAX_BUBBLES:
                tail = bubbles[_MAX_BUBBLES - 1:]
                bubbles = bubbles[: _MAX_BUBBLES - 1] + ["\n\n".join(tail)]
        else:
            # Long message path: no paragraph split; chunk via truncate_message.
            chunks = self.truncate_message(formatted, max_length=MAX_SENDBLUE_LENGTH)
            if len(chunks) > _MAX_BUBBLES:
                tail_chunks = chunks[_MAX_BUBBLES - 1:]
                chunks = chunks[: _MAX_BUBBLES - 1] + ["\n\n".join(tail_chunks)]
            bubbles = chunks

        # Per-bubble: apply 4000-char cap and strip (N/M) indicators.
        _NM_RE = re.compile(r"\s\(\d+/\d+\)$")
        final_bubbles: List[str] = []
        for bubble in bubbles:
            capped = self.truncate_message(bubble, max_length=MAX_SENDBLUE_LENGTH)
            # Use only the first chunk (cap); strip (N/M) suffix if present.
            chunk = capped[0] if capped else bubble
            chunk = _NM_RE.sub("", chunk)
            if len(capped) > 1:
                # Content was dropped by the cap: say so instead of silence.
                # (Sendblue's real limit is ~19k, so slightly exceeding the
                # cosmetic 4000 cap with the note is safe.)
                chunk = f"{chunk}\n\n{_CUT_SHORT_NOTE}"
            if chunk.strip():
                final_bubbles.append(chunk)

        # Sequential sends with sleep between (not after the last one).
        last_result = SendResult(success=True)
        for idx, bubble in enumerate(final_bubbles):
            result = await self._post_message(chat_id, bubble)
            if not result.success:
                return result
            last_result = result
            if idx < len(final_bubbles) - 1:
                await asyncio.sleep(0.8)
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

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file (upload to Sendblue, then send its media_url)."""
        return await self._send_local_media(chat_id, image_path, caption)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local document/file via Sendblue."""
        return await self._send_local_media(chat_id, file_path, caption)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send audio as an inline iMessage voice note.

        iMessage voice bubbles want a .caf/opus file, so convert first (ffmpeg is
        in the image). Fall back to the original audio as a plain attachment if
        conversion fails.
        """
        path = await self._to_caf(audio_path) or audio_path
        return await self._send_local_media(chat_id, path, caption)

    async def _send_local_media(
        self, chat_id: str, file_path: str, caption: Optional[str]
    ) -> SendResult:
        """Upload a local file to Sendblue, then send it as media."""
        try:
            media_url = await self._upload_media_file(file_path)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[sendblue] media upload failed for %s: %s",
                redact_phone(chat_id),
                exc,
            )
            return SendResult(success=False, error=str(exc), retryable=True)
        return await self._post_message(
            chat_id, self.format_message(caption or ""), media_url=media_url
        )

    async def _upload_media_file(self, file_path: str) -> str:
        """Upload a local file to Sendblue; return the hosted media_url.

        POST /api/upload-file -- multipart form (field 'file') + auth headers.
        Response: {"status": "OK", "media_url": "...", "mediaObjectId": "..."}.
        """
        import aiohttp

        session = self._http_session
        if session is None:
            raise RuntimeError("Sendblue HTTP session not initialized")
        with open(file_path, "rb") as fh:
            form = aiohttp.FormData()
            form.add_field("file", fh, filename=os.path.basename(file_path))
            # Auth headers only; let aiohttp set the multipart Content-Type/boundary.
            headers = {
                "sb-api-key-id": self._api_key_id,
                "sb-api-secret-key": self._api_secret_key,
            }
            async with session.post(
                f"{SENDBLUE_API_BASE}{UPLOAD_FILE_PATH}",
                data=form,
                headers=headers,
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status >= 400 or not isinstance(body, dict):
                    msg = ""
                    if isinstance(body, dict):
                        msg = body.get("message") or body.get("error_message") or ""
                    raise RuntimeError(f"upload-file {resp.status}: {msg or 'failed'}")
                media_url = body.get("media_url", "")
                if not media_url:
                    raise RuntimeError("upload-file returned no media_url")
                return str(media_url)

    async def _to_caf(self, audio_path: str) -> Optional[str]:
        """Convert audio to .caf/opus for an inline iMessage voice note.

        Returns the .caf path on success, or None to fall back to the original.
        """
        if audio_path.lower().endswith(".caf"):
            return audio_path
        base = audio_path.rsplit(".", 1)[0] if "." in audio_path else audio_path
        out_path = f"{base}.caf"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", audio_path,
                "-acodec", "opus", "-b:a", "24k", out_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0 and os.path.exists(out_path):
                return out_path
            logger.warning("[sendblue] ffmpeg .caf conversion exit=%s", proc.returncode)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sendblue] .caf conversion failed: %s", exc)
        return None

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
        except Exception as exc:  # noqa: BLE001 -- typing is non-critical
            logger.debug("[sendblue] typing indicator error: %s", exc)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    def format_message(self, content: str) -> str:
        """Humanize gateway notices, then strip markdown for iMessage."""
        # 1. Rewrite known gateway boilerplate into texting-voice strings.
        content = _humanize_gateway_notice(content)
        # 2. Core markdown stripping (bold, italic, code fences, headings, links).
        content = strip_markdown(content)
        # 3. Extended stripping (bullets, blockquotes, hr, tables, em-dash, name tags).
        content = _strip_markdown_extra(content)
        return content

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

        # Shared-secret gate (Sendblue does not sign requests) -- constant-time.
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
        media_url = str(payload.get("media_url", "") or "").strip()

        # Ignore echoes of our own number and non-inbound events.
        if payload.get("is_outbound"):
            return web.json_response({"ok": True})
        if from_number and from_number == self._from_number:
            logger.debug("[sendblue] ignoring echo from own number")
            return web.json_response({"ok": True})
        # Allow media-only messages (a photo/voice note with no caption).
        if not from_number or (not content and not media_url):
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

        safe_content = (content[:80] or f"[media {media_url[:60]}]").replace(
            "\n", "\\n"
        ).replace("\r", "\\r")
        logger.info(
            "[sendblue] inbound from %s: %s",
            redact_phone(from_number),
            safe_content,
        )

        # Resolve a friendly name for the sender (falls back to the number).
        # chat_id / user_id stay the raw E.164 so routing, sessions, and
        # pairing/auth (which match on user_id) are unchanged -- only the
        # display name the agent sees is enriched.
        display_name = self._user_names.get(from_number, from_number)
        source = self.build_source(
            chat_id=from_number,
            chat_name=display_name,
            chat_type="dm",
            user_id=from_number,
            user_name=display_name,
        )

        # Download inbound media so the agent can see images (native vision) or
        # handle audio. Best-effort: on failure, fall back to a text-only event.
        media_urls: List[str] = []
        media_types: List[str] = []
        msg_type = MessageType.TEXT
        if media_url:
            ctype = str(
                payload.get("media_type") or payload.get("content_type") or ""
            ).strip().lower()
            is_audio = ctype.startswith("audio/") or media_url.lower().endswith(
                (".caf", ".m4a", ".mp3", ".aac", ".ogg", ".wav", ".amr")
            )
            try:
                if is_audio:
                    local = await cache_audio_from_url(media_url)
                    media_types.append(ctype or "audio/m4a")
                    if not content:
                        msg_type = MessageType.VOICE
                else:
                    local = await cache_image_from_url(media_url)
                    media_types.append(ctype or "image/jpeg")
                    if not content:
                        msg_type = MessageType.PHOTO
                media_urls.append(local)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[sendblue] failed to cache inbound media: %s", exc)

        # Tag the inbound text with the sender so the agent always knows who it
        # is replying to (DMs never get the gateway's group sender-prefix). Skip
        # slash commands so "/help", "/new", etc. still parse from the start.
        # Uses raw ``content`` only here -- the media/type detection above already
        # ran against the unprefixed content.
        if content.startswith("/"):
            event_text = content
        else:
            event_text = f"[{display_name}] {content}".rstrip()

        event = MessageEvent(
            text=event_text,
            message_type=msg_type,
            source=source,
            raw_message=payload,
            message_id=message_id,
            media_urls=media_urls,
            media_types=media_types,
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

    # Humanize + strip markdown + extended strip, same pipeline as format_message().
    message = _humanize_gateway_notice(message)
    message = strip_markdown(message)
    message = _strip_markdown_extra(message)

    # Empty after processing: skip API call entirely (e.g. suppressed heartbeat failure).
    if not message.strip():
        return {"success": True, "platform": "sendblue", "chat_id": chat_id, "message_id": ""}

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
    except Exception as exc:  # noqa: BLE001 -- avoid leaking phone/URL from exc text
        return {"error": f"Sendblue send failed: {type(exc).__name__}"}


def _build_adapter(config):
    """Factory wrapper that constructs SendblueAdapter from a PlatformConfig."""
    return SendblueAdapter(config)


def register(ctx) -> None:
    """Plugin entry point -- called by the Hermes plugin system at startup."""
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
            "Markdown -- asterisks and hashes show up literally, so use plain text. "
            "Keep replies concise and conversational, like real text messages; long "
            "answers are split into multiple bubbles. Sendblue auto-falls back "
            "iMessage -> RCS -> SMS, so avoid relying on iMessage-only rich features. "
            "You can attach media by returning MEDIA:/absolute/path/to/file."
        ),
    )
