"""每天把整份记忆备份到一个 GitHub 私有仓库。

用 GitHub 的「建 / 改文件」接口：每次把导出的 JSON 写成仓库里的同一个文件，
一天一个提交，历史版本都在 git 记录里。只往私有仓库写——仓库是公开的就拒绝（记忆不能公开）。

要配的：MELE_MEMORY_BACKUP_REPO（owner/name）、MELE_MEMORY_BACKUP_TOKEN
（细粒度 token，只给这一个仓库 Contents 读写）、可选 MELE_MEMORY_BACKUP_HOUR（用户时区几点，默认 4）。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

import memory as M

log = logging.getLogger("mele_memory.backup")
API = "https://api.github.com"
PATH = "mele-memory.json"


@dataclass
class BackupState:
    """最近一次备份的结果，状态页看。只记在这个进程里，重启就清空。"""
    last_at: str | None = None
    ok: bool | None = None
    message: str = ""
    commit_url: str = ""
    history: list = field(default_factory=list)


class BackupError(Exception):
    pass


def _headers(token: str) -> dict:
    return {"authorization": f"Bearer {token}", "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28", "user-agent": "mele-memory-backup"}


async def backup_once(pool, user_id, *, repo: str, token: str, branch: str = "", client: httpx.AsyncClient | None = None,
                      now: datetime | None = None) -> dict:
    """备份一次，返回 {commit_url, bytes}。仓库不存在、不是私有、钥匙不对，都抛 BackupError。"""
    now = now or datetime.now(timezone.utc)
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        info = await client.get(f"{API}/repos/{repo}", headers=_headers(token))
        if info.status_code in (401, 403):
            raise BackupError("GitHub token rejected (check it has Contents read/write on this repo)")
        if info.status_code == 404:
            raise BackupError(f"repo {repo} not found (or the token can't see it)")
        info.raise_for_status()
        meta = info.json()
        if not meta.get("private"):
            raise BackupError(f"repo {repo} is public; memories only go to a private repo")
        branch = branch or meta.get("default_branch") or "main"

        data = await M.export(pool, user_id)
        data["exported_at"] = now.isoformat()
        body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")

        cur = await client.get(f"{API}/repos/{repo}/contents/{PATH}", params={"ref": branch}, headers=_headers(token))
        sha = cur.json().get("sha") if cur.status_code == 200 else None
        payload = {"message": f"backup {now:%Y-%m-%d %H:%M} UTC", "branch": branch,
                   "content": base64.b64encode(body).decode("ascii")}
        if sha:
            payload["sha"] = sha
        put = await client.put(f"{API}/repos/{repo}/contents/{PATH}", json=payload, headers=_headers(token))
        if put.status_code not in (200, 201):
            raise BackupError(f"GitHub refused the write: HTTP {put.status_code} {put.text[:200]}")
        return {"commit_url": put.json().get("commit", {}).get("html_url", ""), "bytes": len(body)}
    except httpx.HTTPError as e:
        raise BackupError(f"network error: {e}") from e
    finally:
        if own:
            await client.aclose()


async def run_and_record(state: BackupState, pool, user_id, **kw) -> BackupState:
    at = datetime.now(timezone.utc).isoformat()
    try:
        r = await backup_once(pool, user_id, **kw)
        state.ok, state.message, state.commit_url = True, f"{r['bytes']} bytes", r["commit_url"]
        log.info("backup ok: %s", r["commit_url"])
    except BackupError as e:
        state.ok, state.message, state.commit_url = False, str(e), ""
        log.warning("backup failed: %s", e)
    state.last_at = at
    state.history = ([{"at": at, "ok": state.ok, "message": state.message}] + state.history)[:10]
    return state


def next_run(now: datetime, hour: int, tz: str) -> datetime:
    """下一次几点（用户时区）的 UTC 时间。"""
    local = now.astimezone(ZoneInfo(tz))
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target = (local + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return target.astimezone(timezone.utc)


async def daily_loop(state: BackupState, pool, user_id, *, repo: str, token: str, hour: int, tz: str,
                     branch: str = "") -> None:
    while True:
        wait = (next_run(datetime.now(timezone.utc), hour, tz) - datetime.now(timezone.utc)).total_seconds()
        await asyncio.sleep(max(1.0, wait))
        await run_and_record(state, pool, user_id, repo=repo, token=token, branch=branch)
