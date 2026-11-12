"""The Anthropic picker must order by release, not by a hand-maintained catalog row.

``/v1/models`` publishes ``line`` and ``created_at`` for every row, so the newest release of a
line can lead the picker without anyone editing ``_PROVIDER_MODELS["anthropic"]``. Before this
contract the curated row led the merge, so a release missing from that static list (e.g. a new
Sonnet) rendered behind older 4.x rows — and vanished outright when the live probe failed.

Regression for the curated-first ordering. Covers the sort inside ``_fetch_anthropic_models``
and the merge in ``_anthropic_catalog``.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

from hermes_cli import models as M


def _row(model_id: str, created_at: str, line: str | None = None) -> dict:
    row = {"id": model_id, "type": "model", "created_at": created_at}
    if line:
        row["line"] = line
    return row


ROWS = [
    _row("claude-sonnet-5", "2026-06-29T00:00:00Z", "sonnet"),
    _row("claude-sonnet-5-5", "2026-09-28T00:00:00Z", "sonnet"),
    _row("claude-sonnet-4-6", "2026-02-17T00:00:00Z", "sonnet"),
    _row("claude-opus-5", "2026-07-24T00:00:00Z", "opus"),
    _row("claude-opus-5-5", "2026-09-21T00:00:00Z", "opus"),
    _row("claude-haiku-5-5", "2026-10-07T00:00:00Z", "haiku"),
    # No metadata at all: must not lead its line.
    {"id": "claude-sonnet-undated", "type": "model"},
]


class _ModelsHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        raw = json.dumps({"data": ROWS, "has_more": False, "last_id": ROWS[-1]["id"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture()
def models_server():
    srv = HTTPServer(("127.0.0.1", 0), _ModelsHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def test_fetch_orders_newest_release_first_inside_each_line(models_server):
    got = M._fetch_anthropic_models(base_url=models_server, api_key="sk-test")

    assert got is not None
    sonnet = [m for m in got if "sonnet" in m]
    assert sonnet[0] == "claude-sonnet-5-5", got
    assert got.index("claude-sonnet-5-5") < got.index("claude-sonnet-5")
    assert got.index("claude-sonnet-5") < got.index("claude-sonnet-4-6")
    # An undated row sorts behind every dated row of its line rather than jumping to the top.
    assert got.index("claude-sonnet-4-6") < got.index("claude-sonnet-undated")


def test_fetch_groups_lines_opus_sonnet_haiku(models_server):
    got = M._fetch_anthropic_models(base_url=models_server, api_key="sk-test")

    assert got is not None
    assert got.index("claude-opus-5-5") < got.index("claude-sonnet-5-5") < got.index("claude-haiku-5-5")


def test_live_only_newer_release_leads_the_picker_over_older_curated_rows():
    """A release absent from the static catalog still appears at its release position."""
    newer = "claude-sonnet-6"
    live = ["claude-sonnet-6", "claude-sonnet-5-5", "claude-sonnet-5"]
    with patch.object(M, "_fetch_anthropic_models", return_value=live):
        result = M.provider_model_ids("anthropic")

    assert result.index(newer) < result.index("claude-sonnet-5")
    assert result.count(newer) == 1
