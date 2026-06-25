"""Tests for the hermes_cli.nous_billing HTTP client's response handling.

Focus: a 2xx response with a NON-JSON body (e.g. a reverse-proxy / SPA fallback
HTML page when a route isn't actually serving the billing API) must surface as a
typed BillingError, NOT a raw json.JSONDecodeError that escapes the typed-error
contract and reads downstream as "not logged in".
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager

import pytest

from hermes_cli import nous_billing as nb


class _FakeResp(io.BytesIO):
    """Minimal urlopen() context-manager stand-in with a .status attribute."""

    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@contextmanager
def _stub(monkeypatch, body: bytes, status: int = 200):
    # Bypass auth/token resolution entirely — we only exercise response parsing.
    monkeypatch.setattr(nb, "_resolve_token_and_base", lambda **kw: ("tok", "https://portal.example"))
    monkeypatch.setattr(nb, "_token_cache", None, raising=False)
    monkeypatch.setattr(nb.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(body, status))
    yield


def test_non_json_2xx_body_raises_typed_billing_error(monkeypatch):
    # A 200 that returns an HTML page (route not actually mounted) must NOT crash
    # with json.JSONDecodeError — it becomes a typed, non-auth BillingError.
    html = b"<!DOCTYPE html><html><head><title>Not Found</title></head></html>"
    with _stub(monkeypatch, html, status=200):
        with pytest.raises(nb.BillingError) as ei:
            nb.get_subscription_state()
    exc = ei.value
    # Not the auth subclass — this is "endpoint unavailable", not "logged out".
    assert not isinstance(exc, nb.BillingAuthError)
    assert getattr(exc, "error", None) == "endpoint_unavailable"


def test_empty_2xx_body_returns_empty_dict(monkeypatch):
    with _stub(monkeypatch, b"", status=200):
        assert nb.get_billing_state() == {}


def test_valid_json_2xx_body_parses(monkeypatch):
    payload = {"org": {"name": "Acme"}, "balanceUsd": "10"}
    with _stub(monkeypatch, json.dumps(payload).encode(), status=200):
        assert nb.get_billing_state() == payload
