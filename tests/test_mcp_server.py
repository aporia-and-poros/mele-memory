import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import memory as M
from mcp_server.server import Service, Settings, build_http_app, build_server, check_http, main
from memory.embed import FakeEmbedder

E = FakeEmbedder()
TOKEN = "t" * 32


@pytest.fixture
def server(pool, user_a):
    return build_server(Service(pool=pool, embedder=E, user_id=user_a, names=("Lumi", "小满")))


async def call(server, tool, /, **args):
    r = await server.call_tool(tool, args)
    assert not r.is_error, r.content
    return r


async def test_lists_all_tools(server):
    names = {t.name for t in await server.list_tools()}
    assert names == {"remember", "recall", "search", "list_memories", "update", "pin", "resolve", "forget",
                     "person_upsert", "people", "sticky_get", "sticky_set",
                     "date_add", "dates", "date_resolve", "date_forget", "wake", "letter_write", "letter_read"}


async def test_remember_then_recall_surfaces_it(server):
    r = await call(server, "remember", content="小满对芒果过敏，吃了会起疹子", importance=9)
    mid = r.structured_content["memory"]["id"]
    assert "user_id" not in r.structured_content["memory"]
    for filler in ["周末去看了海", "最近在学吉他", "喜欢下雨天在家看书"]:
        await call(server, "remember", content=filler)
    got = await call(server, "recall", text="芒果过敏吃了会起疹子怎么办", level="rich")
    ids = [m["id"] for m in got.structured_content["full"]] + [l["id"] for l in got.structured_content["lines"]]
    assert mid in ids
    assert "芒果" in got.content[0].text


async def test_remember_merges_near_duplicate(server):
    a = await call(server, "remember", content="小满养了一只橘猫叫年糕")
    b = await call(server, "remember", content="小满养了一只橘猫叫年糕")
    assert b.structured_content["merged"] is True
    assert b.structured_content["memory"]["id"] == a.structured_content["memory"]["id"]


async def test_forget_then_search_misses(server):
    r = await call(server, "remember", content="小满的护照号码放在书桌左边抽屉")
    mid = r.structured_content["memory"]["id"]
    hits = await call(server, "search", query="护照 抽屉")
    assert mid in [h["memory"]["id"] for h in hits.structured_content["hits"]]
    await call(server, "forget", id=mid)
    hits = await call(server, "search", query="护照 抽屉")
    assert mid not in [h["memory"]["id"] for h in hits.structured_content["hits"]]
    with pytest.raises(ToolError, match="no memory"):
        await server.call_tool("forget", {"id": mid})


async def test_search_does_not_touch(server, pool, user_a):
    r = await call(server, "remember", content="小满在准备雅思考试")
    mid = r.structured_content["memory"]["id"]
    await call(server, "search", query="雅思")
    m = await M.get(pool, user_a, mid)
    assert m.recall_count == 0 and m.last_recalled_at is None


async def test_update_pin_resolve(server):
    mid = (await call(server, "remember", content="小满下周三要交论文")).structured_content["memory"]["id"]
    r = await call(server, "update", id=mid, content="小满下周四要交论文", importance=8)
    assert r.structured_content["memory"]["content"] == "小满下周四要交论文"
    assert r.structured_content["memory"]["importance"] == 8
    assert (await call(server, "pin", id=mid)).structured_content["memory"]["kind"] == "core"
    assert (await call(server, "pin", id=mid, pinned=False)).structured_content["memory"]["kind"] == "memory"
    assert (await call(server, "resolve", id=mid)).structured_content["memory"]["resolved"] is True
    with pytest.raises(ToolError, match="no memory"):
        await server.call_tool("update", {"id": 999999, "content": "x"})


async def test_list_memories_filters_kind(server):
    await call(server, "remember", content="小满喜欢喝燕麦拿铁")
    mid = (await call(server, "remember", content="小满的生日是五月二十号")).structured_content["memory"]["id"]
    await call(server, "pin", id=mid)
    core = await call(server, "list_memories", kind="core")
    assert [m["id"] for m in core.structured_content["memories"]] == [mid]
    everything = await call(server, "list_memories")
    assert len(everything.structured_content["memories"]) == 2
    with pytest.raises(ToolError):
        await server.call_tool("list_memories", {"kind": "nonsense"})


async def test_ai_cannot_overwrite_user_facts(server, pool, user_a):
    await M.upsert_person(pool, E, user_a, "阿杰", relation="室友", facts="对花生过敏", by="user")
    r = await call(server, "person_upsert", name="阿杰", relation="同事", content="不过敏",
                   impression="说话很冲但人不坏", aliases=["杰哥"])
    p = r.structured_content["person"]
    assert p["relation"] == "室友" and p["content"] == "对花生过敏"     # 用户写的事实没被改
    assert p["impression"] == "说话很冲但人不坏" and "杰哥" in p["aliases"]
    assert p["updated_by"] == "ai"
    listed = await call(server, "people")
    assert [x["name"] for x in listed.structured_content["people"]] == ["阿杰"]


async def test_ai_can_edit_its_own_card(server):
    await call(server, "person_upsert", name="Mia", relation="同学", content="在读建筑")
    r = await call(server, "person_upsert", name="mia", content="在读建筑，明年毕业")
    assert r.structured_content["person"]["content"] == "在读建筑，明年毕业"


async def test_sticky(server):
    assert (await call(server, "sticky_get")).structured_content["text"] == ""
    await call(server, "sticky_set", text="下次问问她面试怎么样")
    assert (await call(server, "sticky_get")).structured_content["text"] == "下次问问她面试怎么样"


async def test_users_are_isolated(pool, user_a, user_b):
    a = build_server(Service(pool=pool, embedder=E, user_id=user_a))
    b = build_server(Service(pool=pool, embedder=E, user_id=user_b))
    mid = (await call(a, "remember", content="只属于 A 的秘密")).structured_content["memory"]["id"]
    assert (await call(b, "list_memories")).structured_content["memories"] == []
    with pytest.raises(ToolError):
        await b.call_tool("forget", {"id": mid})


def test_settings_from_env():
    s = Settings.from_env({"MELE_MEMORY_NAMES": " Lumi, 小满 ,,", "MELE_MEMORY_FAKE_EMBED": "1",
                           "MELE_MEMORY_USER_NAME": "小满, 满满",
                           "MELE_MEMORY_USER": "12345678-1234-5678-1234-567812345678"})
    assert s.names == ("Lumi", "小满") and s.fake_embed and s.user_from_env
    assert s.user_names == ("小满", "满满")
    assert str(s.user_id) == "12345678-1234-5678-1234-567812345678"
    d = Settings.from_env({})
    assert d.dsn == "postgresql://localhost/mele_memory" and not d.user_from_env and not d.fake_embed


def test_http_without_token_generates_one(monkeypatch, tmp_path):
    """没配钥匙不再拒绝启动：生成一把存进数据目录，下次还是这把。"""
    import mcp_server.server as srv
    monkeypatch.delenv("MELE_MEMORY_TOKEN", raising=False)
    monkeypatch.setenv("MELE_MEMORY_DATA", str(tmp_path))
    seen = []
    monkeypatch.setattr(srv, "serve", lambda settings, **kw: seen.append(settings.token))
    monkeypatch.setattr(srv.asyncio, "run", lambda x: None)
    main(["--http"])
    main(["--http"])
    assert len(seen) == 2 and seen[0] == seen[1] == (tmp_path / "token").read_text().strip()
    with pytest.raises(SystemExit, match="MELE_MEMORY_TOKEN"):
        check_http(Settings(token=""))
    with pytest.raises(SystemExit, match="at least"):
        check_http(Settings(token="short"))
    check_http(Settings(token=TOKEN))


async def test_http_requires_bearer(pool, user_a):
    svc = Service(pool=pool, embedder=E, user_id=user_a)
    app = build_http_app(build_server(svc), svc, Settings(token=TOKEN))
    inner = app.app
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}}
    accept = {"accept": "application/json, text/event-stream"}
    async with inner.router.lifespan_context(inner):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as c:
            assert (await c.get("/health")).status_code == 200
            assert (await c.post("/mcp", json=init, headers=accept)).status_code == 401
            bad = {**accept, "authorization": "Bearer " + "x" * 32}
            assert (await c.post("/mcp", json=init, headers=bad)).status_code == 401
            ok = await c.post("/mcp", json=init, headers={**accept, "authorization": f"Bearer {TOKEN}"})
            assert ok.status_code == 200 and "mele-memory" in ok.text


async def test_remember_with_title_and_tags(server):
    r = await call(server, "remember", content="小满下周四交护理论文", title="交论文", tags=["学业", "#论文"])
    m = r.structured_content["memory"]
    assert m["title"] == "交论文" and m["tags"] == ["学业", "论文"]
    assert "#学业 #论文" in r.content[0].text and "交论文｜" in r.content[0].text
    u = await call(server, "update", id=m["id"], tags=["毕业"], title="交毕业论文")
    assert u.structured_content["memory"]["tags"] == ["毕业"]
    assert u.structured_content["memory"]["title"] == "交毕业论文"


async def test_dates_surface_in_recall_then_resolve(server, pool, user_a):
    from datetime import timedelta
    from mcp_server.server import Service
    today = Service(pool=pool, embedder=E, user_id=user_a).today()
    d = await call(server, "date_add", day=(today + timedelta(days=1)).isoformat(), title="面试", at_time="10:00")
    did = d.structured_content["date"]["id"]
    with pytest.raises(ToolError, match="day must look like"):
        await server.call_tool("date_add", {"day": "明天", "title": "x"})
    got = await call(server, "recall", text="随便聊聊今天天气")
    assert got.structured_content["coming_up"] == ["Tomorrow: 面试 (10:00)"]
    assert "Coming up" in got.content[0].text
    listed = await call(server, "dates")
    assert [x["id"] for x in listed.structured_content["dates"]] == [did]
    await call(server, "date_resolve", id=did, result="顺利，下周二面")
    assert (await call(server, "dates")).structured_content["dates"] == []
    mems = (await call(server, "list_memories")).structured_content["memories"]
    assert any("顺利，下周二面" in m["content"] for m in mems)
    assert (await call(server, "recall", text="随便聊聊今天天气")).structured_content["coming_up"] == []
    await call(server, "date_forget", id=did)
    with pytest.raises(ToolError, match="no date"):
        await server.call_tool("date_forget", {"id": did})


def test_backup_settings_must_pair(monkeypatch):
    monkeypatch.setenv("MELE_MEMORY_BACKUP_REPO", "someone/backup")
    monkeypatch.delenv("MELE_MEMORY_BACKUP_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="both"):
        main([])
    monkeypatch.setenv("MELE_MEMORY_BACKUP_TOKEN", "t")
    monkeypatch.setenv("MELE_MEMORY_BACKUP_HOUR", "25")
    with pytest.raises(SystemExit, match="0-23"):
        main([])


def test_bad_timezone_refused(monkeypatch):
    monkeypatch.setenv("MELE_MEMORY_TZ", "Mars/Olympus")
    with pytest.raises(SystemExit, match="time zone"):
        main([])


async def test_wake_and_letters(server, pool, user_a):
    assert (await call(server, "wake")).content[0].text == "Nothing yet. This is the beginning."
    core = (await call(server, "remember", content="小满对芒果过敏", importance=9)).structured_content["memory"]["id"]
    await call(server, "pin", id=core)
    await call(server, "remember", content="小满下周四交论文，还没写完", importance=7)
    await call(server, "sticky_set", text="问问论文写到哪了")
    await call(server, "letter_write", content="她今天很累，明天先别提论文", title="给明天的我")
    await M.write_letter(pool, user_a, "考完请你吃蛋糕", author="user")
    w = await call(server, "wake")
    text = w.content[0].text
    for bit in ("from your past self", "from the user", "芒果过敏", "交论文", "问问论文写到哪了"):
        assert bit in text, bit
    assert text.index("明天先别提论文") < text.index("吃蛋糕")              # 信按时间先后读
    assert len(w.structured_content["letters"]) == 2
    assert (await call(server, "wake")).structured_content["letters"] == []   # 读过就不再递
    assert len((await call(server, "letter_read")).structured_content["letters"]) == 2
    assert len((await call(server, "letter_read", author="user")).structured_content["letters"]) == 1
    with pytest.raises(ToolError):
        await server.call_tool("letter_write", {"content": "  "})


async def test_list_memories_order_and_resolved(server):
    a = (await call(server, "remember", content="周一去游泳", importance=3)).structured_content["memory"]["id"]
    b = (await call(server, "remember", content="芒果过敏", importance=9)).structured_content["memory"]["id"]
    await call(server, "resolve", id=a)
    r = await call(server, "list_memories", order="important")
    assert [m["id"] for m in r.structured_content["memories"]] == [b, a]
    assert [m["id"] for m in (await call(server, "list_memories", resolved=True)).structured_content["memories"]] == [a]
    with pytest.raises(ToolError):
        await server.call_tool("list_memories", {"order": "random"})


LINK = "L" * 40


async def test_link_mode(pool, user_a):
    svc = Service(pool=pool, embedder=E, user_id=user_a)
    app = build_http_app(build_server(svc), svc, Settings(token=TOKEN, link_secret=LINK))
    inner = app.app
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    accept = {"accept": "application/json, text/event-stream"}
    async with inner.router.lifespan_context(inner):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as c:
            ok = await c.post(f"/mcp/{LINK}", json=init, headers=accept)
            assert ok.status_code == 200 and "mele-memory" in ok.text
            assert (await c.post("/mcp/" + "M" * 40, json=init, headers=accept)).status_code == 401
            assert (await c.post(f"/mcp/{LINK}/x", json=init, headers=accept)).status_code == 401
            assert (await c.get(f"/api/memories")).status_code == 401              # 链接只开 MCP
            assert (await c.post("/mcp", json=init, headers=accept)).status_code == 401


def test_link_secret_rules():
    with pytest.raises(SystemExit, match="at least 32"):
        check_http(Settings(token=TOKEN, link_secret="short"))
    with pytest.raises(SystemExit, match="differ"):
        check_http(Settings(token="a" * 40, link_secret="a" * 40))
    with pytest.raises(SystemExit, match="letters and digits"):
        check_http(Settings(token=TOKEN, link_secret="a/" * 20))
    check_http(Settings(token=TOKEN, link_secret=LINK))


def test_access_log_redacts_link():
    import logging
    from mcp_server.server import RedactSecret
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', ("1.2.3.4", "POST", f"/mcp/{LINK}"), None)
    RedactSecret(LINK).filter(rec)
    assert LINK not in rec.getMessage() and "/mcp/***" in rec.getMessage()
