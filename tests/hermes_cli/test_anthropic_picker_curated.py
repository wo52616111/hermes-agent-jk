"""The Anthropic picker merge must never drop reachable curated aliases — and must not let them
bury newer live releases.

Bug — curated-first ordering buried new releases
    ``_anthropic_catalog`` merged ``_PROVIDER_MODELS["anthropic"]`` *ahead of* the live
    ``/v1/models`` dump, so a model Anthropic had already enumerated (but nobody had added to
    the static list) rendered behind older 4.x rows in every picker surface.

Contract now: the live dump leads (it is account-scoped, authoritative, and carries
``created_at``); curated entries only fill ids the dump has not enumerated yet — an alias
Anthropic routes before listing it still shows up, and no id appears twice.
"""

from unittest.mock import patch

from hermes_cli import models as M


def test_anthropic_merge_dedupes_overlap_and_appends_curated_only_aliases():
    """Live rows lead; curated-only aliases trail; overlap appears once."""
    live = [
        "claude-sonnet-5-5",        # live-only: new release, not in the curated row
        "claude-opus-4-8",          # overlaps curated
        "claude-sonnet-4-6",        # overlaps curated
    ]
    with patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.provider_model_ids("anthropic")

    assert result.count("claude-opus-4-8") == 1
    # The live order is preserved verbatim at the head of the picker.
    assert result[: len(live)] == live
    # Curated-only aliases are still offered, after the live rows.
    curated_only = [m for m in M._PROVIDER_MODELS["anthropic"] if m not in live]
    assert curated_only, "fixture assumption: the curated row has ids live does not list"
    assert result[len(live):] == curated_only


def test_anthropic_falls_back_to_curated_when_live_unavailable():
    """No creds / live failure -> curated list verbatim (alias still present)."""
    with patch.object(M, "_fetch_anthropic_models", return_value=None):
        result = M.provider_model_ids("anthropic")

    assert result == list(M._PROVIDER_MODELS["anthropic"])
