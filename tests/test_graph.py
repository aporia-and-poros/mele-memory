import importlib

from memory import people, store

# memory.graph 这个名字被对外同名函数占了，这里要的是模块本身
graph = importlib.import_module("memory.graph")
from memory.embed import FakeEmbedder

E = FakeEmbedder()


async def test_graph_nodes_and_edges(pool, user_a):
    a = (await store.remember(pool, E, user_a, "年糕是一只橘猫，四岁")).memory
    b = (await store.remember(pool, E, user_a, "年糕是橘猫，上周打了疫苗")).memory
    c = (await store.remember(pool, E, user_a, "Mia is training for a half marathon")).memory
    p = await people.upsert_person(pool, E, user_a, "阿杰", relation="好朋友")
    d = (await store.remember(pool, E, user_a, "和阿杰约好圣诞去塔斯马尼亚")).memory

    g = await graph.graph(pool, user_a, min_sim=0.3)
    ids = {n.id for n in g.nodes}
    assert ids == {a.id, b.id, c.id, p.id, d.id}
    pairs = {(e.kind, frozenset((e.source, e.target))) for e in g.edges}
    assert ("similar", frozenset((a.id, b.id))) in pairs
    assert ("person", frozenset((p.id, d.id))) in pairs
    assert not any(c.id in pair for kind, pair in pairs if kind == "similar")
    label = {n.id: n.label for n in g.nodes}
    assert label[p.id] == "阿杰"


async def test_graph_empty(pool, user_a):
    g = await graph.graph(pool, user_a)
    assert g.nodes == [] and g.edges == []
    assert g.to_dict() == {"nodes": [], "edges": [], "hubs": [], "user": None}


def _n(i, kind="memory", tags=(), label=""):
    return graph.GraphNode(id=i, label=label or str(i), kind=kind, weight=0.5, tags=list(tags))


def test_find_hubs_tags_people_and_sun():
    nodes = [_n(i, tags=["健康"] if i <= 7 else ["猫"] if i <= 9 else []) for i in range(1, 21)]
    nodes += [_n(100, "person", label="小满"), _n(101, "person", label="年糕")]
    edges = [graph.GraphEdge(100, i, "person", 1.0) for i in range(1, 13)]      # 小满挂着 12 / 20 条
    edges += [graph.GraphEdge(101, 8, "person", 1.0), graph.GraphEdge(101, 9, "person", 1.0)]
    hubs = graph.find_hubs(nodes, edges)
    assert [(h.key, len(h.members), h.sun) for h in hubs] == [("person:100", 12, True), ("tag:健康", 7, False)]
    assert hubs[0].node_id == 100 and hubs[0].label == "小满"
    big = [_n(i, tags=["健康"] if i <= 7 else []) for i in range(1, 201)]       # 200 条：门槛变成 10
    assert graph.find_hubs(big, []) == []


async def test_graph_has_hubs(pool, user_a):
    for text in ["周二去游泳", "早上跑了五公里", "报了瑜伽课", "膝盖跑步会疼", "买了新球拍", "周末爬山看日出"]:
        await store.remember(pool, E, user_a, text, tags=["运动"])
    g = await graph.graph(pool, user_a)
    assert [h.label for h in g.hubs] == ["运动"] and len(g.hubs[0].members) == 6
    assert g.to_dict()["hubs"][0]["kind"] == "tag"


async def test_user_at_the_center(pool, user_a):
    a = (await store.remember(pool, E, user_a, "小满对芒果过敏")).memory
    b = (await store.remember(pool, E, user_a, "年糕上周打了疫苗")).memory
    card = await people.upsert_person(pool, E, user_a, "小满", relation="TA 自己")
    g = await graph.graph(pool, user_a, user_names=["小满"])
    assert g.user.label == "小满" and g.user.members == [a.id] and g.user.node_id == card.id
    assert all(h.node_id != card.id for h in g.hubs)
    assert (await graph.graph(pool, user_a)).user is None          # 没名字、也没有挂一半以上的卡


def test_sun_becomes_user_without_names():
    nodes = [_n(i) for i in range(1, 11)] + [_n(100, "person", label="小满")]
    edges = [graph.GraphEdge(100, i, "person", 1.0) for i in range(1, 8)]
    user, rest = graph.find_user(nodes, graph.find_hubs(nodes, edges), {i: "" for i in range(1, 11)})
    assert user.label == "小满" and user.node_id == 100 and len(user.members) == 7 and rest == []
