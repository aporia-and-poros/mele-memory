import httpx
import pytest

import memory as M
from mcp_server.server import Service, Settings, build_http_app, build_server
from memory.embed import FakeEmbedder

E = FakeEmbedder()
TOKEN = "w" * 32
AUTH = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture
async def client(pool, user_a):
    svc = Service(pool=pool, embedder=E, user_id=user_a)
    app = build_http_app(build_server(svc), svc, Settings(token=TOKEN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as c:
        yield c


async def add(pool, user, text, **kw):
    return (await M.remember(pool, E, user, text, **kw)).memory


async def test_api_needs_token_but_page_does_not(client):
    assert (await client.get("/api/memories")).status_code == 401
    assert (await client.delete("/api/memories/1")).status_code == 401
    page = await client.get("/")
    assert page.status_code == 200 and "<html" in page.text.lower()


async def test_list_paginates_and_filters(client, pool, user_a):
    for i in range(5):
        await add(pool, user_a, f"第{i}件事：{'春夏秋冬雪'[i]}天去了{'北京上海广州深圳成都'[i*2:i*2+2]}")
    core = await add(pool, user_a, "小满的生日是五月二十号")
    await M.pin(pool, user_a, core.id)
    r = (await client.get("/api/memories?limit=4", headers=AUTH)).json()
    assert len(r["memories"]) == 4 and r["next_offset"] == 4
    assert "user_id" not in r["memories"][0] and "weight" in r["memories"][0]
    r2 = (await client.get("/api/memories?limit=4&offset=4", headers=AUTH)).json()
    assert len(r2["memories"]) == 2 and r2["next_offset"] is None
    only_core = (await client.get("/api/memories?kind=core", headers=AUTH)).json()["memories"]
    assert [m["id"] for m in only_core] == [core.id]
    assert (await client.get("/api/memories?kind=nope", headers=AUTH)).status_code == 400


async def test_patch_content_pin_resolve(client, pool, user_a):
    m = await add(pool, user_a, "小满下周三要交论文")
    r = await client.patch(f"/api/memories/{m.id}", headers=AUTH,
                           json={"content": "小满下周四要交论文", "importance": 8, "pinned": True, "resolved": True})
    assert r.status_code == 200
    got = r.json()["memory"]
    assert got["content"] == "小满下周四要交论文" and got["importance"] == 8
    assert got["kind"] == "core" and got["resolved"] is True
    for bad in ({}, {"importance": 11}, {"importance": True}, {"content": "  "}, {"pinned": "yes"}, {"kind": "core"}):
        assert (await client.patch(f"/api/memories/{m.id}", headers=AUTH, json=bad)).status_code == 400, bad
    assert (await client.patch("/api/memories/999999", headers=AUTH, json={"resolved": True})).status_code == 404


async def test_person_cards_cannot_be_pinned(client, pool, user_a):
    p = await M.upsert_person(pool, E, user_a, "阿杰", relation="室友")
    assert (await client.patch(f"/api/memories/{p.id}", headers=AUTH, json={"pinned": True})).status_code == 400
    people = (await client.get("/api/people", headers=AUTH)).json()["people"]
    assert [x["name"] for x in people] == ["阿杰"]


async def test_delete(client, pool, user_a):
    m = await add(pool, user_a, "要删掉的一条")
    assert (await client.delete(f"/api/memories/{m.id}", headers=AUTH)).json() == {"deleted": m.id}
    assert await M.get(pool, user_a, m.id) is None
    assert (await client.delete(f"/api/memories/{m.id}", headers=AUTH)).status_code == 404


async def test_graph(client, pool, user_a):
    await add(pool, user_a, "小满喜欢猫，养了一只橘猫")
    await add(pool, user_a, "小满的橘猫叫年糕，很喜欢猫")
    await M.upsert_person(pool, E, user_a, "年糕", relation="猫")
    g = (await client.get("/api/graph", headers=AUTH)).json()
    assert len(g["nodes"]) == 3
    assert {e["kind"] for e in g["edges"]} >= {"person"}


async def test_other_users_memories_invisible(client, pool, user_b):
    m = await add(pool, user_b, "B 的记忆")
    assert (await client.get("/api/memories", headers=AUTH)).json()["memories"] == []
    assert (await client.delete(f"/api/memories/{m.id}", headers=AUTH)).status_code == 404
    assert (await client.patch(f"/api/memories/{m.id}", headers=AUTH, json={"resolved": True})).status_code == 404


async def test_patch_title_and_tags(client, pool, user_a):
    m = await add(pool, user_a, "小满下周三要交论文")
    r = (await client.patch(f"/api/memories/{m.id}", headers=AUTH,
                            json={"title": "交论文", "tags": ["学业", "论文"]})).json()["memory"]
    assert r["title"] == "交论文" and r["tags"] == ["学业", "论文"] and r["content"] == "小满下周三要交论文"
    for bad in ({"tags": "学业"}, {"title": 3}):
        assert (await client.patch(f"/api/memories/{m.id}", headers=AUTH, json=bad)).status_code == 400


async def test_about_is_hidden_from_the_page(client, pool, user_a):
    m = await add(pool, user_a, "TA 最近总熬夜", kind="about")
    assert (await client.get("/api/memories?kind=about", headers=AUTH)).status_code == 403
    assert (await client.get("/api/memories", headers=AUTH)).json()["memories"] == []
    assert (await client.patch(f"/api/memories/{m.id}", headers=AUTH, json={"resolved": True})).status_code == 404
    assert (await client.delete(f"/api/memories/{m.id}", headers=AUTH)).status_code == 404
    assert await M.get(pool, user_a, m.id) is not None


async def test_dates_api(client, pool, user_a):
    d = await M.add_date(pool, user_a, "2026-10-10", "面试", at_time="10:00")
    r = (await client.get("/api/dates", headers=AUTH)).json()
    assert [x["title"] for x in r["dates"]] == ["面试"] and "today" in r
    p = await client.patch(f"/api/dates/{d.id}", headers=AUTH, json={"title": "二面", "at_time": "14:30"})
    assert p.json()["date"]["title"] == "二面" and p.json()["date"]["at_time"] == "14:30"
    assert (await client.patch(f"/api/dates/{d.id}", headers=AUTH, json={"day": "明天"})).status_code == 400
    assert (await client.patch(f"/api/dates/{d.id}", headers=AUTH, json={"resolved": True})).status_code == 200
    assert (await client.get("/api/dates", headers=AUTH)).json()["dates"] == []
    assert len((await client.get("/api/dates?resolved=1", headers=AUTH)).json()["dates"]) == 1
    assert (await client.delete(f"/api/dates/{d.id}", headers=AUTH)).json() == {"deleted": d.id}
    assert (await client.patch(f"/api/dates/{d.id}", headers=AUTH, json={"note": "x"})).status_code == 404


async def test_get_one_memory(client, pool, user_a):
    m = await add(pool, user_a, "小满喜欢秋天")
    assert (await client.get(f"/api/memories/{m.id}", headers=AUTH)).json()["memory"]["content"] == "小满喜欢秋天"
    hidden = await add(pool, user_a, "TA 最近总熬夜", kind="about")
    assert (await client.get(f"/api/memories/{hidden.id}", headers=AUTH)).status_code == 404
    assert (await client.get("/api/memories/999999", headers=AUTH)).status_code == 404


async def test_graph_has_user_and_ring(pool, user_a):
    svc = Service(pool=pool, embedder=E, user_id=user_a, user_names=("小满",))
    app = build_http_app(build_server(svc), svc, Settings(token=TOKEN))
    a = await add(pool, user_a, "小满对芒果过敏")
    await add(pool, user_a, "年糕上周打了疫苗")
    await M.remember(pool, E, user_a, "今天去看了海，风很大", kind="diary", title="看海")
    await M.add_date(pool, user_a, "2026-10-10", "面试")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as c:
        g = (await c.get("/api/graph", headers=AUTH)).json()
    assert g["user"]["label"] == "小满" and g["user"]["members"] == [a.id]
    assert sorted((r["type"], r["label"]) for r in g["ring"]) == [("date", "面试"), ("diary", "看海")]


async def test_user_writes_person_cards(client, pool, user_a):
    r = await client.post("/api/people", headers=AUTH, json={"name": "秀兰", "relation": "妈妈",
                                                            "content": "生日 3 月 14 日", "aliases": ["妈"]})
    p = r.json()["person"]
    assert p["created_by"] == "user" and p["aliases"] == ["妈"]
    ai = await M.upsert_person(pool, E, user_a, "秀兰", facts="生日 4 月 1 日", impression="很温柔", by="ai")
    assert ai.content == "生日 3 月 14 日" and ai.impression == "很温柔"      # 用户写的事实它改不动
    u = await client.patch(f"/api/people/{p['id']}", headers=AUTH, json={"content": "生日 3 月 14 日，喜欢君子兰"})
    assert u.json()["person"]["content"] == "生日 3 月 14 日，喜欢君子兰" and u.json()["person"]["impression"] == "很温柔"
    for bad in ({"name": "  "}, {"aliases": "妈"}, {"age": 50}):
        assert (await client.post("/api/people", headers=AUTH, json=bad)).status_code == 400, bad
    assert (await client.patch("/api/people/999999", headers=AUTH, json={"relation": "x"})).status_code == 404


async def test_order_resolved_letters_export_status(client, pool, user_a):
    a = await add(pool, user_a, "周一去游泳", importance=3)
    b = await add(pool, user_a, "芒果过敏", importance=9)
    await M.resolve(pool, user_a, a.id)
    get = lambda q: client.get("/api/memories?" + q, headers=AUTH)
    assert [m["id"] for m in (await get("order=important")).json()["memories"]] == [b.id, a.id]
    assert [m["id"] for m in (await get("order=old")).json()["memories"]] == [a.id, b.id]
    assert [m["id"] for m in (await get("resolved=1")).json()["memories"]] == [a.id]
    assert (await get("order=nope")).status_code == 400

    w = await client.post("/api/letters", headers=AUTH, json={"content": "考完请你吃蛋糕", "title": "约定"})
    assert w.json()["letter"]["author"] == "user"
    await M.write_letter(pool, user_a, "明天先别提论文")
    xs = (await client.get("/api/letters", headers=AUTH)).json()["letters"]
    assert [x["author"] for x in xs] == ["ai", "user"] and all(x["read_at"] is None for x in xs)   # 网页看信不算读过
    assert (await client.post("/api/letters", headers=AUTH, json={"content": " "})).status_code == 400
    assert (await client.delete(f"/api/letters/{xs[1]['id']}", headers=AUTH)).status_code == 200

    hidden = await add(pool, user_a, "TA 最近总熬夜", kind="about")
    ex = await client.get("/api/export", headers=AUTH)
    assert "attachment" in ex.headers["content-disposition"]
    assert hidden.id in [m["id"] for m in ex.json()["memories"]] and len(ex.json()["letters"]) == 1
    assert (await client.get("/api/export")).status_code == 401

    st = (await client.get("/api/status", headers=AUTH)).json()
    assert st["total"] == 3 and st["resolved"] == 1 and st["letters"] == 1 and st["embedder"] == "FakeEmbedder"


async def test_backup_endpoint_when_not_configured(client):
    r = await client.post("/api/backup", headers=AUTH)
    assert r.status_code == 409
    st = (await client.get("/api/status", headers=AUTH)).json()
    assert st["backup"]["configured"] is False and "token" not in st["backup"]
