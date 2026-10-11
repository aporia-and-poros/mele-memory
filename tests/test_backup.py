"""备份：用假的 GitHub（httpx.MockTransport），不碰网络。"""
import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

import memory as M
from mcp_server.backup import BackupError, BackupState, backup_once, next_run, run_and_record
from memory.embed import FakeEmbedder

E = FakeEmbedder()
REPO = "someone/memory-backup"


def fake_github(private=True, existing_sha=None, put_status=201, auth_ok=True):
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if not auth_ok:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if req.method == "GET" and req.url.path == f"/repos/{REPO}":
            return httpx.Response(200, json={"private": private, "default_branch": "main"})
        if req.method == "GET" and req.url.path.endswith("/contents/mele-memory.json"):
            return httpx.Response(200, json={"sha": existing_sha}) if existing_sha else httpx.Response(404, json={})
        if req.method == "PUT":
            return httpx.Response(put_status, json={"commit": {"html_url": "https://github.com/x/commit/1"}})
        return httpx.Response(404, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


async def test_backup_writes_export(pool, user_a):
    await M.remember(pool, E, user_a, "小满对芒果过敏", importance=9)
    await M.write_letter(pool, user_a, "给明天的我")
    client, calls = fake_github(existing_sha="abc123")
    r = await backup_once(pool, user_a, repo=REPO, token="t", client=client,
                          now=datetime(2026, 10, 4, 20, tzinfo=timezone.utc))
    assert r["commit_url"].endswith("/commit/1")
    put = next(c for c in calls if c.method == "PUT")
    body = json.loads(put.content)
    assert body["sha"] == "abc123" and body["branch"] == "main" and body["message"] == "backup 2026-10-04 20:00 UTC"
    data = json.loads(base64.b64decode(body["content"]))
    assert [m["content"] for m in data["memories"]] == ["小满对芒果过敏"] and len(data["letters"]) == 1
    assert put.headers["authorization"] == "Bearer t"


async def test_first_backup_has_no_sha(pool, user_a):
    client, calls = fake_github()
    await backup_once(pool, user_a, repo=REPO, token="t", client=client)
    assert "sha" not in json.loads(next(c for c in calls if c.method == "PUT").content)


async def test_refuses_public_repo(pool, user_a):
    client, calls = fake_github(private=False)
    with pytest.raises(BackupError, match="public"):
        await backup_once(pool, user_a, repo=REPO, token="t", client=client)
    assert not any(c.method == "PUT" for c in calls)                 # 一个字都没写出去


async def test_bad_token_recorded(pool, user_a, monkeypatch):
    client, _ = fake_github(auth_ok=False)
    st = await run_and_record(BackupState(), pool, user_a, repo=REPO, token="bad", client=client)
    assert st.ok is False and "token" in st.message and st.last_at and len(st.history) == 1


def test_next_run():
    now = datetime(2026, 10, 4, 19, 30, tzinfo=timezone.utc)       # 上海 10-05 03:30
    assert next_run(now, 4, "Asia/Shanghai") == datetime(2026, 10, 4, 20, 0, tzinfo=timezone.utc)
    assert next_run(now, 3, "Asia/Shanghai") == datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc)
