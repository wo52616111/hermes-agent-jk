"""A same-provider recovery retry must go out with the credential the pool rotated TO.

Regression: the recovery ladder rebuilt the retry client from ``resolved_api_key`` -- the key
captured *before* ``_recover_provider_pool`` rotated the pool. The retry therefore re-sent the
exhausted credential, failed again, and that second failure (reported with no key identity) was
blamed on the entry the rotation had just selected -- the healthy account, benched for an hour.

Two seams are covered with real code and no network: the retry builder (only the client builder is
stubbed, and the assertion is on the key it receives) and the credential rung of the real recovery
ladder (driven with a scripted driver, the pattern from test_auxiliary_auth_rung_fallthrough).
"""

from typing import Any
from unittest.mock import MagicMock

import agent.auxiliary_client as aux

RELAY = "https://opencode.ai/zen/go/v1"
FAILED_KEY = "failed-key"
ROTATED_KEY = "rotated-key"
AUX_MODEL = "deepseek-v4.1-flash"


class _ApiError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def _payment_error():
    return _ApiError("Error code: 402 - Insufficient account funds", status_code=402)


class _FakeEntry:
    def __init__(self, key: str):
        self.runtime_api_key = key
        self.access_token = key


class _FakePool:
    """Minimal pool double: its entries plus the entry rotation landed on."""

    def __init__(self, keys, current=None):
        self._entries = [_FakeEntry(k) for k in keys]
        self._current = _FakeEntry(current) if current else None

    def has_credentials(self) -> bool:
        return bool(self._entries)

    def entries(self):
        return list(self._entries)

    def current(self):
        return self._current


# --------------------------------------------------------------------------- retry builder


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
        resolved_provider="opencode-go", resolved_model=AUX_MODEL,
        resolved_base_url=RELAY, resolved_api_mode="chat_completions", main_runtime=None,
        final_model=AUX_MODEL, messages=[{"role": "user", "content": "hi"}],
        temperature=None, max_tokens=None, tools=None, effective_timeout=30.0,
        effective_extra_body={}, reasoning_config=None, async_mode=False,
    )


def test_chat_retry_rebuilds_with_the_rotated_credential(monkeypatch):
    captured = {}
    _stub_build(monkeypatch, captured)
    monkeypatch.setattr(aux, "_effective_provider_for_client", lambda client, provider: provider)

    aux._prepare_same_provider_retry(
        task=None, resolved_api_key=FAILED_KEY, rotated_api_key=ROTATED_KEY, **_retry_kwargs())

    assert captured["api_key"] == ROTATED_KEY


def test_vision_retry_rebuilds_with_the_rotated_credential(monkeypatch):
    """The vision rung is a sibling of the chat rung: both must rebind the credential."""
    captured = {}
    client = MagicMock()
    client.base_url = RELAY

    def fake_vision(provider=None, model=None, base_url=None, api_key=None, async_mode=False,
                    **kwargs):
        captured["api_key"] = api_key
        return provider, client, model
    monkeypatch.setattr(aux, "resolve_vision_provider_client", fake_vision)

    aux._prepare_same_provider_retry(
        task="vision", resolved_api_key=FAILED_KEY, rotated_api_key=ROTATED_KEY, **_retry_kwargs())

    assert captured["api_key"] == ROTATED_KEY


def test_retry_without_a_rotation_keeps_the_resolved_key(monkeypatch):
    """No rotated credential (e.g. the OAuth-refresh rung) -> the caller's key is still used."""
    captured = {}
    _stub_build(monkeypatch, captured)
    monkeypatch.setattr(aux, "_effective_provider_for_client", lambda client, provider: provider)

    aux._prepare_same_provider_retry(task=None, resolved_api_key="standalone-key", **_retry_kwargs())

    assert captured["api_key"] == "standalone-key"


# ------------------------------------------------------- which credential the retry may use


def test_rotated_key_comes_from_the_recovered_providers_pool(monkeypatch):
    monkeypatch.setattr(aux, "load_pool",
                        lambda provider: _FakePool(["key-1", "key-2"], current="key-2"))

    assert aux._pool_rotated_api_key("opencode-go", "key-1") == "key-2"


def test_rotated_key_leaves_a_credential_the_pool_does_not_own(monkeypatch):
    """An explicit task/route key must not be silently swapped for a pool credential."""
    monkeypatch.setattr(aux, "load_pool",
                        lambda provider: _FakePool(["key-1", "key-2"], current="key-2"))

    assert aux._pool_rotated_api_key("opencode-go", "explicit-config-key") == ""


def test_rotated_key_is_empty_without_a_pool(monkeypatch):
    monkeypatch.setattr(aux, "load_pool", lambda provider: None)

    assert aux._pool_rotated_api_key("opencode-go", "key-1") == ""
    assert aux._pool_rotated_api_key("", "key-1") == ""


# --------------------------------------------------------------- the real credential rung


def _fake_provider_fallback(first_err, route):
    yield from ()
    return "chain-response"


def _ladder(*, resolved_provider, resolved_api_key, client):
    return aux._aux_recovery_ladder(
        _payment_error(), client=client, kwargs={"model": AUX_MODEL}, task="compression",
        async_mode=False, base_info="https://relay.example/v1", resolved_provider=resolved_provider,
        resolved_model=AUX_MODEL, resolved_base_url=RELAY, resolved_api_key=resolved_api_key,
        resolved_api_mode=None, final_model=AUX_MODEL, max_tokens=None, main_runtime=None,
        route_info={},
    )


def test_rotation_retry_carries_the_pool_credential_even_on_an_auto_route(monkeypatch):
    """An ``auto`` route names no provider, so the key must come from the RECOVERED provider.

    Re-deriving it from ``resolved_provider`` finds no pool at all -- which is how the retry kept
    re-sending the failed key on the path that actually hit this bug (an auxiliary call inheriting
    the session's route).
    """
    client = MagicMock()
    client.api_key = FAILED_KEY
    client.base_url = RELAY
    rotations = []
    monkeypatch.setattr(aux, "_recoverable_pool_provider", lambda *a, **kw: "opencode-go")
    monkeypatch.setattr(aux, "_recover_provider_pool",
                        lambda provider, exc, failed_api_key="": (
                            rotations.append((provider, failed_api_key)) or True))
    monkeypatch.setattr(aux, "load_pool",
                        lambda provider: _FakePool([FAILED_KEY, ROTATED_KEY], current=ROTATED_KEY))
    monkeypatch.setattr(aux, "_ladder_provider_fallback", _fake_provider_fallback)

    seen = []

    def perform(step):
        assert step.kind == "retry_same_provider", f"unexpected step {step.kind}"
        seen.append(step.args)
        raise _payment_error()          # the rotated credential hits a wall too

    ladder = _ladder(resolved_provider="auto", resolved_api_key=FAILED_KEY, client=client)
    assert aux._drive_ladder(ladder, perform) == "chain-response"
    assert seen == [("auto", AUX_MODEL, ROTATED_KEY)]
    # The retry's failure is attributed to the credential that actually served it, not guessed
    # from the pool's current entry after the fact.
    assert rotations == [("opencode-go", FAILED_KEY), ("opencode-go", ROTATED_KEY)]
