"""The canonical transport's usage payload carries the capacity row.

`session.usage` / `session.stats` are the only frames a canonical local viewer ever sees, so a
provider's quota travelling only on the legacy backend's snapshot is a silent empty status bar.
Contract: `gateway/session_usage.py::quota_fields` adds `account_usage*` with the same builder the
legacy backend uses (`agent/quota_wire.py`), and both frames validate against the wire models.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

QUOTA_KEYS = {'account_usage', 'account_usage_all', 'account_usage_active', 'account_usage_active_account'}


def _agent(provider='anthropic', base_url='https://api.anthropic.com', api_key='sk-a'):
    compressor = SimpleNamespace(last_prompt_tokens=900, context_length=128000, _config_context_length=128000,
                                 compression_count=1)
    return SimpleNamespace(
        model='fake-model', provider=provider, base_url=base_url, api_key=api_key, session_id='sid',
        session_input_tokens=100, session_output_tokens=40, session_cache_read_tokens=0,
        session_cache_write_tokens=0, session_prompt_tokens=115, session_completion_tokens=40,
        session_total_tokens=155, session_reasoning_tokens=3, session_api_calls=2,
        context_compressor=compressor, _api_latency_history=[], _api_output_history=[],
        get_rate_limit_state=lambda: None, _session_messages=[], _turn_base_usage_anchor=None)


def _group(provider='anthropic', fetched_at='2026-10-18T05:17:18+00:00', age_s=None):
    return {'provider': provider, 'fetched_at': fetched_at, 'age_s': age_s, 'error': None,
            'backoff_until': None, 'hint': None, 'multi_account': False,
            'windows': [{'period': '5h', 'used_percent': 12.0, 'reset_at': '2026-10-18T09:00:00+00:00',
                         'rolled': None, 'status': None, 'window_seconds': 18000}]}


@pytest.fixture
def quota(monkeypatch):
    """One stored provider group, without touching the real quota store."""
    from agent import quota_wire
    groups = [_group()]
    monkeypatch.setattr(quota_wire, 'all_groups', lambda: list(groups))
    monkeypatch.setattr(quota_wire, 'active_fingerprint', lambda provider, api_key=None: 'fp-active')
    return groups


def test_quota_fields_carry_every_provider_group_and_the_sessions_active_account(quota):
    from gateway.session_usage import quota_fields
    fields = quota_fields(_agent())
    assert set(fields) == QUOTA_KEYS
    assert [g['provider'] for g in fields['account_usage_all']] == ['anthropic']
    assert fields['account_usage']['provider'] == 'anthropic'
    assert fields['account_usage']['windows'][0]['used_percent'] == 12.0
    assert fields['account_usage_active'] == 'anthropic'
    assert fields['account_usage_active_account'] == 'fp-active'


def test_opencode_zen_pool_reads_as_the_opencode_go_group(quota):
    from gateway.session_usage import quota_fields
    agent = _agent(provider='opencode', base_url='https://opencode.ai/zen/go/v1')
    assert quota_fields(agent)['account_usage_active'] == 'opencode-go'


def test_group_age_is_recomputed_at_read_time(quota):
    """A snapshot's stored ``age_s`` must not freeze: the bar has to age while the session idles."""
    from agent import quota_wire
    fetched = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    quota[:] = [_group(fetched_at=fetched, age_s=0)]
    aged = quota_wire.fields(_agent())['account_usage_all'][0]
    assert aged['age_s'] >= 3500


def test_a_failed_quota_read_leaves_the_usage_payload_untouched(monkeypatch):
    from agent import quota_wire
    from gateway.session_usage import quota_fields

    def boom():
        raise OSError('quota store unavailable')
    monkeypatch.setattr(quota_wire, 'all_groups', boom)
    fields = quota_fields(_agent())
    assert fields == {}
    assert not (set(fields) & QUOTA_KEYS)


def test_published_usage_frame_validates_with_the_capacity_row(quota):
    from gateway.config import Platform
    from gateway.session_usage import publish_usage
    from tests.gateway.test_local_viewer_usage import _turn as viewer_turn
    from tui_gateway.contracts.registry import EVENTS

    published = []
    agent = _agent()
    turn = viewer_turn(Platform.LOCAL, published, agent)

    publish_usage(turn, agent)
    kind, payload = published[-1]
    assert kind == 'session.usage'
    assert payload['account_usage_all'][0]['provider'] == 'anthropic'
    assert payload['account_usage_active'] == 'anthropic'
    EVENTS['session.usage'].payload.model_validate(payload)
