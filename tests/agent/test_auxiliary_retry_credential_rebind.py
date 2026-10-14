"""A same-provider recovery retry must go out with the pool's CURRENT credential.

Regression (#113022): the auxiliary recovery ladder rebuilt the retry client from
``resolved_api_key`` -- the key captured *before* ``_recover_provider_pool`` rotated the pool.
The retry therefore re-sent the exhausted credential, failed again, and the pool then blamed
(and benched) the healthy account it had just rotated to. Real code, no network: only the client
builder and the pool peek are stubbed, and the assertion is on the key the builder receives.
"""

from typing import Any
from unittest.mock import MagicMock

import agent.auxiliary_client as aux

RELAY = "https://opencode.ai/zen/go/v1"


def _stub_build(monkeypatch, captured, *, return_client=None):
    def fake_resolve(provider, model=None, async_mode=False, explicit_base_url=None,
                     explicit_api_key=None, **kwargs):
        captured["provider"] = provider
        captured["api_key"] = explicit_api_key
        client = return_client or MagicMock()
        client.base_url = RELAY
        return client, model
    monkeypatch.setattr(aux, "resolve_provider_client", fake_resolve)
    monkeypatch.setattr(aux, "_client_cache", {})


def _retry_kwargs() -> Any:
    return dict(
        resolved_provider="opencode-go", resolved_model="deepseek-v4.1-flash",
        resolved_base_url=RELAY, resolved_api_mode="chat_completions", main_runtime=None,
        final_model="deepseek-v4.1-flash", messages=[{"role": "user", "content": "hi"}],
        temperature=None, max_tokens=None, tools=None, effective_timeout=30.0,
        effective_extra_body={}, reasoning_config=None, async_mode=False,
    )


def test_chat_retry_rebuilds_with_the_pools_current_credential(monkeypatch):
    captured = {}
    _stub_build(monkeypatch, captured)
    monkeypatch.setattr(aux, "_effective_provider_for_client", lambda client, provider: provider)
    monkeypatch.setattr(aux, "_peek_pool_entry",
                        lambda provider, pool=None: MagicMock(runtime_api_key="rotated-key"))

    aux._prepare_same_provider_retry(task=None, resolved_api_key="failed-key", **_retry_kwargs())

    assert captured["api_key"] == "rotated-key"


def test_vision_retry_rebuilds_with_the_pools_current_credential(monkeypatch):
    """The vision rung is a sibling of the chat rung: both must rebind the credential."""
    captured = {}
    client = MagicMock()
    client.base_url = RELAY

    def fake_vision(provider=None, model=None, base_url=None, api_key=None, async_mode=False,
                    **kwargs):
        captured["api_key"] = api_key
        return provider, client, model
    monkeypatch.setattr(aux, "resolve_vision_provider_client", fake_vision)
    monkeypatch.setattr(aux, "_peek_pool_entry",
                        lambda provider, pool=None: MagicMock(runtime_api_key="rotated-key"))

    aux._prepare_same_provider_retry(task="vision", resolved_api_key="failed-key", **_retry_kwargs())

    assert captured["api_key"] == "rotated-key"


def test_retry_without_a_pool_keeps_the_resolved_key(monkeypatch):
    """No pool for the provider -> the caller's resolved key is still the authority."""
    captured = {}
    _stub_build(monkeypatch, captured)
    monkeypatch.setattr(aux, "_effective_provider_for_client", lambda client, provider: provider)
    monkeypatch.setattr(aux, "_peek_pool_entry", lambda provider, pool=None: None)

    aux._prepare_same_provider_retry(task=None, resolved_api_key="standalone-key", **_retry_kwargs())

    assert captured["api_key"] == "standalone-key"
