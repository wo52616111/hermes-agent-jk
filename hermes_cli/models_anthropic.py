"""Anthropic model catalog: ``/v1/models`` page plumbing and picker ordering.

``/v1/models`` is cursor-paginated and publishes ``line`` (opus/sonnet/haiku/fable) plus
``created_at`` on every row, so the picker can lead with the newest release of each line without a
curated-catalog edit. Pure helpers only — no imports from :mod:`hermes_cli.models`, which imports
*this* module.

Pitfall this replaces: ordering by id leaves a freshly shipped version at the tail of the picker.
``claude-sonnet-5`` sorts below ``claude-sonnet-5-5`` only because the curated row predates it, and
a curated-first merge put every live-only release behind the whole hand-maintained list — which
reads as "the newest Sonnet is 5".
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Optional

# Picker order between product lines. Ids carry the line in their name, so an older or proxied
# response without the ``line`` field still groups correctly.
_ANTHROPIC_LINES = ("opus", "sonnet", "haiku", "fable")

# Safety bound on cursor pages: the live catalog is ~20 rows, so this only stops a looping server.
_ANTHROPIC_MODELS_MAX_PAGES = 20


def _anthropic_models_url(base_url: Optional[str] = None, *, after_id: Optional[str] = None) -> str:
    """Anthropic ``/v1/models`` page URL. The endpoint defaults to a page of 20 (smaller than the
    live catalog), so every request asks for the maximum page size and ``after_id`` continues from
    a previous page's ``last_id``."""
    endpoint = str(base_url or "https://api.anthropic.com").strip().rstrip("/")
    url = endpoint + ("/models" if endpoint.endswith("/v1") else "/v1/models")
    params = {"limit": "1000"}
    if after_id:
        params["after_id"] = after_id
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)


def _anthropic_next_cursor(page: Any, seen_cursors: set[str]) -> Optional[str]:
    """``last_id`` to continue from, or None when the page is final or the server repeats a
    cursor (which would otherwise loop forever)."""
    if not isinstance(page, dict) or page.get("has_more") is not True:
        return None
    last_id = page.get("last_id")
    if not isinstance(last_id, str) or not last_id or last_id in seen_cursors:
        return None
    seen_cursors.add(last_id)
    return last_id


def _anthropic_line(row: dict) -> str:
    """The row's ``line``, else the same line inferred from the id (proxies omit the field)."""
    declared = str(row.get("line") or "").strip().lower()
    if declared:
        return declared
    model_id = str(row.get("id") or "").lower()
    return next((line for line in _ANTHROPIC_LINES if line in model_id), "")


def _anthropic_model_sort_key(row: dict) -> tuple[int, int, str]:
    """``(line rank, -release date, id)`` — newest first inside a line, undated rows last."""
    line = _anthropic_line(row)
    rank = _ANTHROPIC_LINES.index(line) if line in _ANTHROPIC_LINES else len(_ANTHROPIC_LINES)
    date = str(row.get("created_at") or "")[:10]
    try:
        # Negated so plain ascending sort yields newest-first; undated rows sort after every
        # dated row in the same line instead of jumping to the top on a parse miss.
        stamp = -int(date.replace("-", ""))
    except ValueError:
        stamp = 0
    return (rank, stamp, str(row.get("id") or ""))
