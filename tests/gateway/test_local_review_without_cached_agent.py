"""``/review`` on a local session whose cached agent is gone.

The gateway drops a session's cached agent after an idle hour, under memory pressure, and around a
``/moa`` turn. ``/review`` read only the cached agent, so a session left alone for an hour answered
"Nothing to review yet — send a message first" until the next message rebuilt one. The conversation
is the session's stored transcript; the reviewer reviews it and its completion re-enters the session.

Real daemon (``python -m gateway.run``), ``slash.exec`` (``session_commands.execute_command``),
loopback model. ``/moa`` drops the cached agent here because the idle sweep's first pass is a minute in.
"""
import asyncio
import json
import signal
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.gateway.fixtures.local_recovery_probe import Model, child_env, daemon, rpc, websocket
from tests.waiting import async_wait_until


@pytest.mark.platforms("linux")  # the daemon is stopped with SIGINT, which raises on Windows
def test_local_review_reviews_the_stored_conversation_once_the_cached_agent_is_gone(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user, work = tmp_path / 'state', tmp_path / 'user', tmp_path / 'work'
    home.mkdir(mode=0o700)
    user.mkdir()
    work.mkdir()
    peer = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    peer.requests = []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'
    (home / 'config.yaml').write_text(json.dumps({
        'gateway': {'multiplex_profiles': False},
        'model': {'provider': 'custom', 'default': 'review-model', 'base_url': url},
        'auxiliary': {'title_generation': {'enabled': False}},
    }))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
               OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=url, PYTHONUNBUFFERED='1')
    out = {}

    async def probe(desc):
        async with websocket(home, desc) as ws:
            created = await rpc(ws, 'session.create', request_id='cold-review', source='cli', toolsets=[],
                                cwd=str(work))
            sid = created['result']['session_id']

            def rows():
                with sqlite3.connect(f'file:{home / "state.db"}?mode=ro', uri=True) as db:
                    return dict(db.execute('SELECT request_id,status FROM session_admissions'))

            async def settle(input_id):
                await async_wait_until(lambda: rows().get(input_id) == 'terminal', timeout=45,
                                       what=f'{input_id} turn terminal')

            await rpc(ws, 'prompt.submit', session_id=sid, input_id='warm', text='OPENED_PR_42')
            await settle('warm')
            moa = (await rpc(ws, 'slash.exec', session_id=sid, command='moa compare the options'))['result']
            await rpc(ws, 'prompt.submit', session_id=sid, input_id='moa', text=moa['message'])
            await settle('moa')
            before = len(peer.requests)
            reply = await rpc(ws, 'slash.exec', session_id=sid, command='review check the naming')
            out['review'] = review = reply.get('result') or reply.get('error')
            assert 'Nothing to review' not in json.dumps(review), ('/review lost the conversation with its agent', out)
            await async_wait_until(lambda: any('check the naming' in json.dumps(r) for r in peer.requests[before:]),
                                   timeout=30, what='the reviewer reaching the model')
            request = next(r for r in peer.requests[before:] if 'check the naming' in json.dumps(r))
            out['reviewer_saw_conversation'] = 'OPENED_PR_42' in json.dumps(request['messages'])
            # The reviewer's completion comes back to this session as a delegation-completion turn.
            await async_wait_until(lambda: any('ASYNC DELEGATION' in json.dumps(r) for r in peer.requests[before:]),
                                   timeout=60, what='the review completion re-entering the session')

    try:
        with daemon(root, home, env, barrier=False) as (proc, desc):
            asyncio.run(probe(desc))
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=20)
    finally:
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=5)

    review = out['review']
    assert review.get('type') == 'exec' and 'Review started' in review['output'], out
    assert out['reviewer_saw_conversation'], out
