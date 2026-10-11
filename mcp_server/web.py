"""网页记忆展示：几个 JSON 接口 + 一个静态页面。跟 MCP 同一个 HTTP 服务、同一把钥匙。

网页这边是用户自己在改，所以写入都算 by="user"。页面本身（/）不要钥匙，它在浏览器里问你要，
再带着 Authorization 调 /api/*。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

import memory as M

from .server import Service, memory_data

WEB_DIR = Path(os.environ.get("MELE_MEMORY_WEB_DIR") or Path(__file__).resolve().parent.parent / "web")
PATCH_FIELDS = {"content", "title", "tags", "importance", "pinned", "resolved"}
DATE_FIELDS = {"day", "at_time", "title", "note", "resolved"}
PERSON_FIELDS = {"name", "aliases", "relation", "content", "impression"}
# 「关于 TA」是它记的、关于用户的小事：照 Mele 的设计，用户在页面上看不到（导出和清空照样带上）
PAGE_HIDDEN = {"about"}
RING_DIARY_MAX = 120   # 星环上最多放几篇日记（最近的）


def _int(request: Request, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(request.query_params.get(key, default))))
    except ValueError:
        return default


def _bad(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def routes(svc: Service) -> list[Route]:
    async def index(_request):
        page = WEB_DIR / "index.html"
        if not page.is_file():
            return PlainTextResponse("web/index.html not found (set MELE_MEMORY_WEB_DIR)", status_code=404)
        return FileResponse(page)

    async def list_memories(request: Request):
        limit = _int(request, "limit", 50, 1, 200)
        offset = _int(request, "offset", 0, 0, 10**9)
        kind = request.query_params.get("kind") or None
        if kind in PAGE_HIDDEN:
            return _bad("this kind is not shown on the page", 403)
        order = request.query_params.get("order") or "new"
        resolved = {"1": True, "true": True, "0": False, "false": False}.get(request.query_params.get("resolved", ""))
        try:
            ms = await M.list_memories(svc.pool, svc.user_id, kind=kind, limit=limit + 1, offset=offset,
                                       order=order, resolved=resolved)
        except ValueError as e:
            return _bad(str(e))
        more = len(ms) > limit
        return JSONResponse({"memories": [memory_data(m) for m in ms[:limit]], "offset": offset,
                             "next_offset": offset + limit if more else None})

    async def get_memory(request: Request):
        m = await M.get(svc.pool, svc.user_id, request.path_params["id"])
        if m is None or m.kind in PAGE_HIDDEN:
            return _bad(f"no memory #{request.path_params['id']}", 404)
        return JSONResponse({"memory": memory_data(m)})

    async def graph(_request):
        """星图 + 星环：没了结的远事和日记绕着记忆球转。"""
        data = (await M.graph(svc.pool, svc.user_id, user_names=svc.user_names)).to_dict()
        ring = [{"type": "date", "id": d.id, "label": d.title, "when": d.day.isoformat()}
                for d in await M.list_dates(svc.pool, svc.user_id)]
        ring += [{"type": "diary", "id": m.id, "label": m.title or m.content[:24], "when": m.created_at.date().isoformat()}
                 for m in await M.list_memories(svc.pool, svc.user_id, kind="diary", limit=RING_DIARY_MAX)]
        data["ring"] = ring
        return JSONResponse(data)

    async def people(_request):
        return JSONResponse({"people": [memory_data(p) for p in await M.list_people(svc.pool, svc.user_id)]})

    async def patch_memory(request: Request):
        mid = request.path_params["id"]
        try:
            body = await request.json()
        except ValueError:
            return _bad("body must be JSON")
        if not isinstance(body, dict) or not body:
            return _bad("body must be a non-empty object")
        if extra := set(body) - PATCH_FIELDS:
            return _bad(f"unknown fields: {', '.join(sorted(extra))}")
        content, importance, title, tags = body.get("content"), body.get("importance"), body.get("title"), body.get("tags")
        if title is not None and not isinstance(title, str):
            return _bad("title must be a string")
        if tags is not None and not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
            return _bad("tags must be a list of strings")
        if content is not None and (not isinstance(content, str) or not content.strip()):
            return _bad("content must be a non-empty string")
        if importance is not None and (not isinstance(importance, int) or isinstance(importance, bool)
                                       or not 1 <= importance <= 10):
            return _bad("importance must be an integer 1-10")
        for key in ("pinned", "resolved"):
            if key in body and not isinstance(body[key], bool):
                return _bad(f"{key} must be true or false")

        m = await M.get(svc.pool, svc.user_id, mid)
        if m is None or m.kind in PAGE_HIDDEN:
            return _bad(f"no memory #{mid}", 404)
        if "pinned" in body and m.kind == "person":
            return _bad("person cards can't be pinned")
        if any(v is not None for v in (content, importance, title, tags)):
            m = await M.update(svc.pool, svc.embedder, svc.user_id, mid, content=content, importance=importance,
                               title=title, tags=tags)
        if "pinned" in body:
            m = await M.pin(svc.pool, svc.user_id, mid, body["pinned"])
        if "resolved" in body:
            m = await M.resolve(svc.pool, svc.user_id, mid, body["resolved"])
        return JSONResponse({"memory": memory_data(m)})

    async def delete_memory(request: Request):
        mid = request.path_params["id"]
        m = await M.get(svc.pool, svc.user_id, mid)
        if m is None or m.kind in PAGE_HIDDEN or not await M.delete(svc.pool, svc.user_id, mid):
            return _bad(f"no memory #{mid}", 404)
        return JSONResponse({"deleted": mid})

    async def _person_body(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return None, _bad("body must be JSON")
        if not isinstance(body, dict) or not body:
            return None, _bad("body must be a non-empty object")
        if extra := set(body) - PERSON_FIELDS:
            return None, _bad(f"unknown fields: {', '.join(sorted(extra))}")
        for key in ("name", "relation", "content", "impression"):
            if key in body and not isinstance(body[key], str):
                return None, _bad(f"{key} must be a string")
        if "aliases" in body and not (isinstance(body["aliases"], list) and all(isinstance(a, str) for a in body["aliases"])):
            return None, _bad("aliases must be a list of strings")
        return body, None

    async def create_person(request: Request):
        """网页上是用户自己在写：by="user"，它以后改不动「是谁 / 要记得」。同名的卡就是改那张。"""
        body, err = await _person_body(request)
        if err:
            return err
        try:
            p = await M.upsert_person(svc.pool, svc.embedder, svc.user_id, body.get("name", ""),
                                      relation=body.get("relation"), facts=body.get("content"),
                                      impression=body.get("impression"), aliases=body.get("aliases") or (), by="user")
        except ValueError as e:
            return _bad(str(e))
        return JSONResponse({"person": memory_data(p)})

    async def patch_person(request: Request):
        body, err = await _person_body(request)
        if err:
            return err
        try:
            p = await M.update_person(svc.pool, svc.embedder, svc.user_id, request.path_params["id"],
                                      name=body.get("name"), aliases=body.get("aliases"), relation=body.get("relation"),
                                      facts=body.get("content"), impression=body.get("impression"), by="user")
        except ValueError as e:
            return _bad(str(e))
        if p is None:
            return _bad(f"no person #{request.path_params['id']}", 404)
        return JSONResponse({"person": memory_data(p)})

    async def list_letters(request: Request):
        """网页上看信不算「读过」：read_at 是留给下一个窗口的它的。"""
        xs = await M.list_letters(svc.pool, svc.user_id, limit=_int(request, "limit", 50, 1, 200),
                                  offset=_int(request, "offset", 0, 0, 10**9))
        return JSONResponse({"letters": [x.to_dict() for x in xs]})

    async def write_letter(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return _bad("body must be JSON")
        if not isinstance(body, dict) or not isinstance(body.get("content"), str) or not isinstance(body.get("title", ""), str):
            return _bad("body must be {content, title?}")
        try:
            x = await M.write_letter(svc.pool, svc.user_id, body["content"], title=body.get("title", ""), author="user")
        except ValueError as e:
            return _bad(str(e))
        return JSONResponse({"letter": x.to_dict()})

    async def delete_letter(request: Request):
        lid = request.path_params["id"]
        if not await M.delete_letter(svc.pool, svc.user_id, lid):
            return _bad(f"no letter #{lid}", 404)
        return JSONResponse({"deleted": lid})

    async def export(_request):
        """一次性导出全部：记忆（含页面上看不到的「关于 TA」和日记）、便利贴、远事、信。"""
        data = await M.export(svc.pool, svc.user_id)
        data["exported_at"] = datetime.now(timezone.utc).isoformat()
        name = f"mele-memory-{datetime.now(timezone.utc):%Y%m%d}.json"
        return JSONResponse(data, headers={"content-disposition": f'attachment; filename="{name}"'})

    async def status(_request):
        st = await M.stats(svc.pool, svc.user_id)
        st["dates_open"] = len(await M.list_dates(svc.pool, svc.user_id))
        st["letters"] = await svc.pool.fetchval("SELECT count(*) FROM letters WHERE user_id = $1", svc.user_id)
        st["letters_unread"] = await svc.pool.fetchval(
            "SELECT count(*) FROM letters WHERE user_id = $1 AND read_at IS NULL", svc.user_id)
        st["sticky"] = await M.get_sticky(svc.pool, svc.user_id)
        st["embedder"] = type(svc.embedder).__name__
        st["user_names"] = list(svc.user_names)
        st["tz"] = svc.tz
        st["today"] = svc.today().isoformat()
        b = svc.backup_state
        st["backup"] = {"configured": bool(svc.backup), "repo": svc.backup.get("repo", ""),   # 钥匙不往外给
                        "hour": svc.backup.get("hour"),
                        "last_at": b.last_at if b else None, "ok": b.ok if b else None,
                        "message": b.message if b else "", "commit_url": b.commit_url if b else ""}
        return JSONResponse(st)

    async def backup_now(_request):
        from .backup import BackupState, run_and_record

        if not svc.backup:
            return _bad("GitHub backup is not configured (MELE_MEMORY_BACKUP_REPO / MELE_MEMORY_BACKUP_TOKEN)", 409)
        if svc.backup_state is None:
            svc.backup_state = BackupState()
        b = await run_and_record(svc.backup_state, svc.pool, svc.user_id, repo=svc.backup["repo"],
                                 token=svc.backup["token"], branch=svc.backup.get("branch", ""))
        return JSONResponse({"ok": b.ok, "message": b.message, "commit_url": b.commit_url, "last_at": b.last_at},
                            status_code=200 if b.ok else 502)

    async def list_dates(request: Request):
        include = request.query_params.get("resolved") in ("1", "true")
        ds = await M.list_dates(svc.pool, svc.user_id, include_resolved=include)
        return JSONResponse({"today": svc.today().isoformat(), "dates": [d.to_dict() for d in ds]})

    async def patch_date(request: Request):
        did = request.path_params["id"]
        try:
            body = await request.json()
        except ValueError:
            return _bad("body must be JSON")
        if not isinstance(body, dict) or not body:
            return _bad("body must be a non-empty object")
        if extra := set(body) - DATE_FIELDS:
            return _bad(f"unknown fields: {', '.join(sorted(extra))}")
        if "resolved" in body and not isinstance(body["resolved"], bool):
            return _bad("resolved must be true or false")
        for key in ("day", "at_time", "title", "note"):
            if key in body and not isinstance(body[key], str):
                return _bad(f"{key} must be a string")
        if await M.get_date(svc.pool, svc.user_id, did) is None:
            return _bad(f"no date #{did}", 404)
        fields = {k: body[k] for k in ("day", "at_time", "title", "note") if k in body}
        try:
            d = await M.update_date(svc.pool, svc.user_id, did, **fields) if fields else None
        except ValueError as e:
            return _bad(str(e))
        if "resolved" in body:
            d = await M.resolve_date(svc.pool, svc.embedder, svc.user_id, did, resolved=body["resolved"])
        return JSONResponse({"date": d.to_dict()})

    async def delete_date(request: Request):
        did = request.path_params["id"]
        if not await M.delete_date(svc.pool, svc.user_id, did):
            return _bad(f"no date #{did}", 404)
        return JSONResponse({"deleted": did})

    return [
        Route("/", index, methods=["GET"]),
        Route("/api/letters", list_letters, methods=["GET"]),
        Route("/api/letters", write_letter, methods=["POST"]),
        Route("/api/letters/{id:int}", delete_letter, methods=["DELETE"]),
        Route("/api/export", export, methods=["GET"]),
        Route("/api/status", status, methods=["GET"]),
        Route("/api/backup", backup_now, methods=["POST"]),
        Route("/api/dates", list_dates, methods=["GET"]),
        Route("/api/dates/{id:int}", patch_date, methods=["PATCH"]),
        Route("/api/dates/{id:int}", delete_date, methods=["DELETE"]),
        Route("/api/memories", list_memories, methods=["GET"]),
        Route("/api/graph", graph, methods=["GET"]),
        Route("/api/people", people, methods=["GET"]),
        Route("/api/people", create_person, methods=["POST"]),
        Route("/api/people/{id:int}", patch_person, methods=["PATCH"]),
        Route("/api/memories/{id:int}", get_memory, methods=["GET"]),
        Route("/api/memories/{id:int}", patch_memory, methods=["PATCH"]),
        Route("/api/memories/{id:int}", delete_memory, methods=["DELETE"]),
    ]
