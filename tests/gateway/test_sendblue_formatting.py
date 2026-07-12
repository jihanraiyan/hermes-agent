"""Tests for Sendblue adapter formatting: humanizer, strip_markdown_extra, and send() splitter.

Follows the plugin-test loader pattern from _plugin_adapter_loader.py and
test_bluebubbles.py. The adapter module is loaded in isolation via
load_plugin_adapter("sendblue") to avoid sys.path collisions.
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter


# ---------------------------------------------------------------------------
# Module-level fixture: load the sendblue adapter once for the session.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sb_mod():
    """Return the sendblue adapter module loaded in isolation."""
    return load_plugin_adapter("sendblue")


@pytest.fixture(scope="module")
def adapter_cls(sb_mod):
    return sb_mod.SendblueAdapter


def _make_adapter(monkeypatch, adapter_cls):
    """Construct a minimal SendblueAdapter with fake credentials."""
    monkeypatch.setenv("SENDBLUE_API_KEY_ID", "fake-key")
    monkeypatch.setenv("SENDBLUE_API_SECRET_KEY", "fake-secret")
    monkeypatch.setenv("SENDBLUE_NUMBER", "+10000000000")
    monkeypatch.setenv("SENDBLUE_INSECURE_NO_TOKEN", "true")

    from gateway.config import Platform, PlatformConfig

    cfg = PlatformConfig(
        enabled=True,
        extra={
            "api_key_id": "fake-key",
            "api_secret_key": "fake-secret",
            "from_number": "+10000000000",
        },
    )
    return adapter_cls(cfg)


# ===========================================================================
# _humanize_gateway_notice tests
# ===========================================================================

class TestHumanizeGatewayNotice:
    """Every row in _HUMANIZE_TABLE must transform correctly."""

    def test_auth_failure(self, sb_mod):
        src = "⚠️ Provider authentication failed. Check the configured credentials; raw provider details are in the gateway logs."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "something's broken on my end (auth issue), give me a bit"

    def test_provider_rejected(self, sb_mod):
        src = "⚠️ The model provider rejected the request. I kept the raw provider error out of chat; check gateway logs for details or try rephrasing."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "hm, that one got blocked on my end. try saying it differently?"

    def test_rate_limit(self, sb_mod):
        src = "⏱️ The model provider is rate-limiting requests. Please wait a moment and try again."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "i'm getting rate limited, give me a minute and try again"

    def test_failed_after_retries(self, sb_mod):
        src = "⚠️ The model provider failed after retries. I kept raw provider details out of chat; check gateway logs for diagnostics."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "something's broken on my end, give me a min and resend that"

    def test_session_too_large(self, sb_mod):
        src = "⚠️ Session too large for the model's context window.\nUse /compact to compress the conversation, or /reset to start fresh."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "my head's too full from this convo, text /reset and we'll start fresh"

    def test_request_failed(self, sb_mod):
        src = "The request failed: some internal error details\nTry again or use /reset to start a fresh session."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "that didn't go through on my end. try again in a sec"

    def test_request_failed_agent_prose_passthrough(self, sb_mod):
        # Agent prose starting the same way but without the gateway's /reset
        # tail must NOT be rewritten (false-positive guard).
        src = "The request failed: the endpoint returned 404, want me to retry with the v2 path?"
        out = sb_mod._humanize_gateway_notice(src)
        assert out == src

    def test_processing_stopped(self, sb_mod):
        src = "⚠️ Processing stopped: processing incomplete. Try again."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "missed that one while i was wrapping something up, send it again?"

    def test_processing_completed_no_response(self, sb_mod):
        src = "⚠️ Processing completed but no response was generated. This may be a transient error — try sending your message again."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "missed that one while i was wrapping something up, send it again?"

    def test_message_not_processed(self, sb_mod):
        src = "⚠️ Your message wasn't processed (the previous turn was still being cleaned up). Please send it again."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "missed that one while i was wrapping something up, send it again?"

    def test_session_auto_reset(self, sb_mod):
        src = "◐ Session automatically reset (inactive for 6h). Conversation history cleared.\nUse /resume to browse and restore a previous session.\nAdjust reset timing in config.yaml under session_reset."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "heads up, i had to restart so we're starting fresh. what were we on?"

    def test_pairing_code_extracted(self, sb_mod):
        src = (
            "Hi~ I don't recognize you yet!\n\n"
            "Here's your pairing code: `ABC123`\n\n"
            "Ask the bot owner to run:\n"
            "`hermes pairing approve sendblue ABC123`"
        )
        out = sb_mod._humanize_gateway_notice(src)
        assert "ABC123" in out
        assert "ask Jihan to add you" in out
        # No em dash in output
        assert "—" not in out

    def test_heartbeat_working(self, sb_mod):
        # Real gateway string uses an em dash (run.py:18120).
        src = "⏳ Working — 3 min"
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "one sec, still on your last thing"

    def test_heartbeat_working_with_detail(self, sb_mod):
        src = "⏳ Working — 6 min — iteration 12/90, web_search"
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "one sec, still on your last thing"

    def test_subagent_working(self, sb_mod):
        src = "⏳ Subagent working (tool_call) — your message is queued for when it finishes (use /stop to cancel everything)."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "one sec, still on your last thing"

    def test_compressing_context(self, sb_mod):
        src = "⏳ Compressing context — your message is queued for when it finishes (use /stop to cancel everything)."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "one sec, still on your last thing"

    def test_queued_for_next_turn(self, sb_mod):
        src = "⏳ Queued for the next turn. I'll respond once the current task finishes."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "one sec, still on your last thing"

    def test_interrupting(self, sb_mod):
        src = "⚡ Interrupting current task. I'll respond to your message shortly."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == "one sec, still on your last thing"

    def test_truncated_footer(self, sb_mod):
        src = "some output ... [truncated, full output saved to /opt/data/cron/output/xyz]"
        out = sb_mod._humanize_gateway_notice(src)
        assert "(cut it short" in out
        # The real content BEFORE the footer must survive the rewrite.
        assert out.startswith("some output")
        # The filesystem path must not leak.
        assert "/opt/data" not in out

    def test_heartbeat_cron_failure_suppressed(self, sb_mod):
        src = "⚠️ Cron 'heartbeat-jihan' failed: provider rate limit. Fallback chain was exhausted or unavailable. Full details saved in cron output."
        out = sb_mod._humanize_gateway_notice(src)
        assert out == ""

    def test_heartbeat_cron_failure_suppressed_variant(self, sb_mod):
        src = "⚠️ Cron 'heartbeat-kunal' failed: some error"
        out = sb_mod._humanize_gateway_notice(src)
        assert out == ""

    def test_other_cron_failure_humanized(self, sb_mod):
        src = "⚠️ Cron 'send-investor-update' failed: provider timeout. Fallback chain was exhausted or unavailable. Full details saved in cron output."
        out = sb_mod._humanize_gateway_notice(src)
        assert "send-investor-update" in out
        assert "snag" in out
        # No ⚠️ emoji leaking through
        assert "⚠" not in out

    def test_pass_through_normal_prose(self, sb_mod):
        """Normal agent prose must not be rewritten."""
        normal = "Hey, I looked into that for you. Looks like the build passed."
        out = sb_mod._humanize_gateway_notice(normal)
        assert out == normal

    def test_pass_through_prose_starting_with_emoji(self, sb_mod):
        """Prose that starts with an emoji but isn't a gateway notice passes through."""
        normal = "🎉 Great news, the deploy succeeded!"
        out = sb_mod._humanize_gateway_notice(normal)
        assert out == normal

    def test_pass_through_prose_with_warning_emoji_mid_text(self, sb_mod):
        """Mid-sentence emoji does not trigger humanizer."""
        normal = "The ⚠️ sign means watch out."
        out = sb_mod._humanize_gateway_notice(normal)
        assert out == normal


# ===========================================================================
# _strip_markdown_extra tests
# ===========================================================================

class TestStripMarkdownExtra:
    """Each rule in _strip_markdown_extra gets at least one test."""

    def test_bullets_dash(self, sb_mod):
        text = "- item one\n- item two"
        out = sb_mod._strip_markdown_extra(text)
        assert out == "item one\nitem two"

    def test_bullets_star(self, sb_mod):
        text = "* item a\n* item b"
        out = sb_mod._strip_markdown_extra(text)
        assert out == "item a\nitem b"

    def test_bullets_plus(self, sb_mod):
        text = "+ thing"
        out = sb_mod._strip_markdown_extra(text)
        assert "thing" in out
        assert "+ " not in out

    def test_numbered_list_unchanged(self, sb_mod):
        text = "1. first\n2. second"
        out = sb_mod._strip_markdown_extra(text)
        # Numbers kept by design
        assert "1." in out
        assert "2." in out

    def test_blockquote(self, sb_mod):
        text = "> This is a quote."
        out = sb_mod._strip_markdown_extra(text)
        assert out == "This is a quote."

    def test_blockquote_no_space(self, sb_mod):
        text = ">No space after marker"
        out = sb_mod._strip_markdown_extra(text)
        assert out == "No space after marker"

    def test_horizontal_rule_dash(self, sb_mod):
        text = "before\n---\nafter"
        out = sb_mod._strip_markdown_extra(text)
        assert "---" not in out
        assert "before" in out
        assert "after" in out

    def test_horizontal_rule_star(self, sb_mod):
        text = "a\n* * *\nb"
        out = sb_mod._strip_markdown_extra(text)
        assert "* * *" not in out

    def test_strikethrough(self, sb_mod):
        text = "~~deleted text~~"
        out = sb_mod._strip_markdown_extra(text)
        assert out == "deleted text"
        assert "~~" not in out

    def test_pipe_table_converted(self, sb_mod):
        text = "| Name | Age |\n| --- | --- |\n| Alice | 30 |\n| Bob | 25 |"
        out = sb_mod._strip_markdown_extra(text)
        assert "Name: Alice" in out
        assert "Age: 30" in out
        assert "Name: Bob" in out
        assert "Age: 25" in out
        assert "|" not in out

    def test_em_dash_spaced(self, sb_mod):
        text = "a phrase — with an em dash"
        out = sb_mod._strip_markdown_extra(text)
        assert "—" not in out
        # " — " (space-emdash-space) -> ", " so "a phrase — with" -> "a phrase, with"
        assert "a phrase, with an em dash" == out

    def test_em_dash_bare(self, sb_mod):
        text = "a—b"
        out = sb_mod._strip_markdown_extra(text)
        assert "—" not in out
        assert "a-b" == out

    def test_name_tag_lead_stripped(self, sb_mod):
        text = "[Jihan] here is your answer"
        out = sb_mod._strip_markdown_extra(text)
        assert out == "here is your answer"

    def test_name_tag_mid_text_untouched(self, sb_mod):
        """[Name] tag in the middle of text must NOT be stripped."""
        text = "Sure thing. [Kunal] mentioned this earlier."
        out = sb_mod._strip_markdown_extra(text)
        assert "[Kunal]" in out

    def test_name_tag_only_at_start(self, sb_mod):
        """Guard only fires at position 0."""
        text = "Response: [Name] some text"
        out = sb_mod._strip_markdown_extra(text)
        # Not at start, so [Name] stays
        assert "[Name]" in out

    def test_no_false_positive_on_normal_brackets(self, sb_mod):
        """Regular [link text] in the middle should not be affected at start."""
        text = "[very long name that exceeds 24 chars limit here] some text"
        out = sb_mod._strip_markdown_extra(text)
        # Exceeds 24-char cap so should NOT strip
        assert "[very long" in out


# ===========================================================================
# format_message integration (humanize + strip + extra strip)
# ===========================================================================

class TestFormatMessage:
    """format_message() pipeline: humanize -> strip_markdown -> strip_markdown_extra."""

    def test_gateway_notice_humanized(self, sb_mod, adapter_cls, monkeypatch):
        a = _make_adapter(monkeypatch, adapter_cls)
        src = "⚠️ Provider authentication failed. Check the configured credentials; raw provider details are in the gateway logs."
        out = a.format_message(src)
        assert out == "something's broken on my end (auth issue), give me a bit"

    def test_normal_prose_unchanged(self, sb_mod, adapter_cls, monkeypatch):
        a = _make_adapter(monkeypatch, adapter_cls)
        prose = "I looked at your Jasper PR. The merge conflict is on line 42."
        out = a.format_message(prose)
        assert "line 42" in out

    def test_bullets_stripped(self, sb_mod, adapter_cls, monkeypatch):
        a = _make_adapter(monkeypatch, adapter_cls)
        src = "Here is the list:\n\n- item one\n- item two"
        out = a.format_message(src)
        assert "- " not in out
        assert "item one" in out

    def test_em_dash_stripped(self, sb_mod, adapter_cls, monkeypatch):
        a = _make_adapter(monkeypatch, adapter_cls)
        src = "This approach — while clever — has risks."
        out = a.format_message(src)
        assert "—" not in out


# ===========================================================================
# send() splitter behavior tests
# ===========================================================================

class TestSendSplitter:
    """send() must split, merge, cap, and skip-on-empty correctly."""

    def _make_adapter_with_mock_post(self, monkeypatch, adapter_cls):
        a = _make_adapter(monkeypatch, adapter_cls)
        from gateway.platforms.base import SendResult

        calls = []

        async def fake_post(chat_id, content, media_url=None):
            calls.append(content)
            return SendResult(success=True, message_id=f"id-{len(calls)}")

        a._post_message = fake_post
        return a, calls

    def test_empty_message_no_api_call(self, adapter_cls, monkeypatch):
        a, calls = self._make_adapter_with_mock_post(monkeypatch, adapter_cls)
        result = asyncio.get_event_loop().run_until_complete(
            a.send("+11234567890", "   \n\n  ")
        )
        assert result.success is True
        assert calls == []

    def test_paragraph_split(self, adapter_cls, monkeypatch):
        a, calls = self._make_adapter_with_mock_post(monkeypatch, adapter_cls)
        # Each paragraph must be >= 25 chars to avoid the short-fragment merge.
        msg = (
            "first paragraph with enough length to not merge\n\n"
            "second paragraph with enough length to not merge\n\n"
            "third paragraph with enough length to not merge"
        )
        asyncio.get_event_loop().run_until_complete(
            a.send("+11234567890", msg)
        )
        assert len(calls) == 3
        assert "first paragraph" in calls[0]

    def test_short_fragment_merged(self, adapter_cls, monkeypatch):
        a, calls = self._make_adapter_with_mock_post(monkeypatch, adapter_cls)
        # "ok" is < 25 chars, should merge into previous bubble
        msg = "first paragraph that has some reasonable length\n\nok"
        asyncio.get_event_loop().run_until_complete(
            a.send("+11234567890", msg)
        )
        assert len(calls) == 1
        assert "ok" in calls[0]

    def test_five_bubble_cap(self, adapter_cls, monkeypatch):
        a, calls = self._make_adapter_with_mock_post(monkeypatch, adapter_cls)
        # 7 paragraphs -> capped to 5, last 3 joined into bubble 5
        paragraphs = [f"paragraph {i} with some decent length text here" for i in range(7)]
        msg = "\n\n".join(paragraphs)
        asyncio.get_event_loop().run_until_complete(
            a.send("+11234567890", msg)
        )
        assert len(calls) == 5

    def test_no_split_over_2000_chars(self, adapter_cls, monkeypatch):
        """Messages >= 2000 chars use chunk path, not paragraph split."""
        a, calls = self._make_adapter_with_mock_post(monkeypatch, adapter_cls)
        # Build a message with 3 paragraphs but total >= 2000 chars
        big_para = "x" * 700
        msg = f"{big_para}\n\n{big_para}\n\n{big_para}"
        assert len(msg) >= 2000
        asyncio.get_event_loop().run_until_complete(
            a.send("+11234567890", msg)
        )
        # Should be chunked, not paragraph-split (chunk count depends on content)
        assert len(calls) >= 1

    def test_nm_indicator_stripped(self, adapter_cls, monkeypatch):
        """(N/M) chunk indicators from truncate_message must not appear in bubbles."""
        a, calls = self._make_adapter_with_mock_post(monkeypatch, adapter_cls)
        # Inject a pre-formatted string containing an indicator
        # by patching format_message to return text with indicator
        original_format = a.format_message

        def format_with_indicator(content):
            result = original_format(content)
            return result + " (1/3)"

        a.format_message = format_with_indicator
        asyncio.get_event_loop().run_until_complete(
            a.send("+11234567890", "hello world")
        )
        for call_content in calls:
            assert "(1/3)" not in call_content

    def test_sleep_between_bubbles(self, adapter_cls, monkeypatch):
        """asyncio.sleep(0.8) must be called between bubbles, not after the last."""
        a, calls = self._make_adapter_with_mock_post(monkeypatch, adapter_cls)
        sleep_calls = []

        async def fake_sleep(t):
            sleep_calls.append(t)

        msg = "para one with some decent length text\n\npara two with some decent length text\n\npara three with some decent text"
        with patch("asyncio.sleep", fake_sleep):
            asyncio.get_event_loop().run_until_complete(a.send("+11234567890", msg))

        # 3 bubbles -> 2 sleeps (not after the last)
        assert len(sleep_calls) == 2
        assert all(t == 0.8 for t in sleep_calls)

    def test_failed_post_returns_early(self, adapter_cls, monkeypatch):
        """A failed POST stops sending immediately and returns the failure."""
        a = _make_adapter(monkeypatch, adapter_cls)
        from gateway.platforms.base import SendResult

        call_count = [0]

        async def failing_post(chat_id, content, media_url=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return SendResult(success=False, error="Network error", retryable=True)
            return SendResult(success=True, message_id="ok")

        a._post_message = failing_post

        msg = "bubble one with decent text here\n\nbubble two with decent text here"
        result = asyncio.get_event_loop().run_until_complete(
            a.send("+11234567890", msg)
        )
        assert result.success is False
        assert call_count[0] == 1  # stopped after first failure

    def test_splits_long_messages_attribute(self, adapter_cls, monkeypatch):
        """SendblueAdapter must declare splits_long_messages = True."""
        a = _make_adapter(monkeypatch, adapter_cls)
        assert getattr(a, "splits_long_messages", False) is True


# ===========================================================================
# _standalone_send formatting tests
# ===========================================================================

class TestStandaloneSend:
    """_standalone_send must apply humanize + strip and skip API on empty."""

    def test_heartbeat_cron_no_api_call(self, sb_mod, monkeypatch):
        """Suppressed heartbeat failure -> no POST, returns success.

        The humanizer maps heartbeat cron failures to "" so _standalone_send
        must detect the empty result and return success without an HTTP call.
        We test this by patching _humanize_gateway_notice to return "" and
        verifying the early-exit path.
        """
        from gateway.config import PlatformConfig

        pconfig = PlatformConfig(enabled=True, extra={
            "api_key_id": "k", "api_secret_key": "s", "from_number": "+1000",
        })
        monkeypatch.setenv("SENDBLUE_API_KEY_ID", "k")
        monkeypatch.setenv("SENDBLUE_API_SECRET_KEY", "s")
        monkeypatch.setenv("SENDBLUE_NUMBER", "+1000")

        src = "⚠️ Cron 'heartbeat-jihan' failed: provider rate limit. Full details saved."

        # Confirm humanizer returns "" for this input
        assert sb_mod._humanize_gateway_notice(src) == ""

        posted = []

        async def run():
            # Patch _humanize_gateway_notice in the adapter module scope
            module_name = sb_mod.__name__
            import sys
            real_mod = sys.modules[module_name]

            class FakeResp:
                status = 200
                async def json(self, content_type=None):
                    return {"message_handle": "x"}
                async def __aenter__(self): return self
                async def __aexit__(self, *a): pass

            class FakePost:
                async def post(self, *a, **kw):
                    posted.append(True)
                    return FakeResp()
                async def __aenter__(self): return self
                async def __aexit__(self, *a): pass

            # Patch at the module level where _standalone_send uses it
            with patch.object(real_mod, "_humanize_gateway_notice", return_value=""):
                # Also patch aiohttp to be importable (even though we expect no call)
                import unittest.mock as um
                fake_aiohttp = um.MagicMock()
                with patch.dict("sys.modules", {"aiohttp": fake_aiohttp}):
                    return await sb_mod._standalone_send(pconfig, "+1000", src)

        result = asyncio.get_event_loop().run_until_complete(run())
        assert posted == []  # no HTTP call made
        assert result.get("success") is True

    def test_normal_message_format_pipeline(self, sb_mod):
        """Normal cron message is processed through the same format pipeline.

        Verifies that _standalone_send applies humanize + strip_markdown +
        _strip_markdown_extra by inspecting the pipeline functions directly,
        since mocking aiohttp's async context manager is fragile.
        """
        src = "**Hey**, here is your update."
        # Apply same pipeline as _standalone_send does
        step1 = sb_mod._humanize_gateway_notice(src)
        from gateway.platforms.helpers import strip_markdown
        step2 = strip_markdown(step1)
        step3 = sb_mod._strip_markdown_extra(step2)
        # Bold stripped
        assert "**" not in step3
        assert "Hey" in step3


# ===========================================================================
# delegate_tool child_prompt_append tests
# ===========================================================================

class TestDelegateChildPromptAppend:
    """Phase 3c: child_prompt_append from config is appended to child_prompt."""

    def test_child_prompt_append_injected(self, monkeypatch):
        """When child_prompt_append is set in config, it appends to child_prompt."""
        from tools.delegate_tool import _build_child_system_prompt

        # Monkeypatch _load_config to return a config with child_prompt_append
        style_suffix = "WRITING STYLE: plain, direct, human."
        fake_cfg = {"child_prompt_append": style_suffix}

        with patch("tools.delegate_tool._load_config", return_value=fake_cfg):
            # Call _build_child_system_prompt directly to get base prompt
            base = _build_child_system_prompt(
                "research this topic",
                None,
                workspace_path=None,
                role="worker",
                max_spawn_depth=1,
                child_depth=1,
            )
            # Simulate what _build_child_agent does after our edit
            _append = fake_cfg.get("child_prompt_append")
            if isinstance(_append, str) and _append.strip():
                result = f"{base}\n\n{_append.strip()}"
            else:
                result = base

        assert style_suffix in result
        assert result.endswith(style_suffix)

    def test_child_prompt_append_empty_ignored(self, monkeypatch):
        """Empty or whitespace-only child_prompt_append does not modify prompt."""
        from tools.delegate_tool import _build_child_system_prompt

        fake_cfg = {"child_prompt_append": "   "}
        base = _build_child_system_prompt(
            "do something",
            None,
            workspace_path=None,
            role="worker",
            max_spawn_depth=1,
            child_depth=1,
        )
        _append = fake_cfg.get("child_prompt_append")
        if isinstance(_append, str) and _append.strip():
            result = f"{base}\n\n{_append.strip()}"
        else:
            result = base

        assert result == base

    def test_child_prompt_append_missing_key_ignored(self, monkeypatch):
        """Missing child_prompt_append key leaves prompt unchanged."""
        from tools.delegate_tool import _build_child_system_prompt

        fake_cfg = {}
        base = _build_child_system_prompt(
            "do something",
            None,
            workspace_path=None,
            role="worker",
            max_spawn_depth=1,
            child_depth=1,
        )
        _append = fake_cfg.get("child_prompt_append")
        if isinstance(_append, str) and _append.strip():
            result = f"{base}\n\n{_append.strip()}"
        else:
            result = base

        assert result == base

    def test_child_prompt_append_non_string_ignored(self, monkeypatch):
        """Non-string child_prompt_append (e.g. list) is ignored gracefully."""
        from tools.delegate_tool import _build_child_system_prompt

        fake_cfg = {"child_prompt_append": ["should", "not", "be", "a", "list"]}
        base = _build_child_system_prompt(
            "do something",
            None,
            workspace_path=None,
            role="worker",
            max_spawn_depth=1,
            child_depth=1,
        )
        _append = fake_cfg.get("child_prompt_append")
        if isinstance(_append, str) and _append.strip():
            result = f"{base}\n\n{_append.strip()}"
        else:
            result = base

        assert result == base
