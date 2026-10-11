"""给 app 的「它记得的」网状页面：每条记忆一个点，点的大小看当前分量；
意思相近的连一条线（每个点最多连 k 个最像的），提到某个人的记忆连到那张人物卡。"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from uuid import UUID

import asyncpg
import numpy as np

from .config import DEFAULT, MemoryConfig
from .decay import weight
from .models import MEMORY_COLUMNS, row_to_memory
from .people import keywords, mentions

LABEL_CHARS = 24


@dataclass
class GraphNode:
    id: int
    label: str
    kind: str
    weight: float
    title: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class GraphEdge:
    source: int
    target: int
    kind: str        # "similar" / "person"
    strength: float


@dataclass
class GraphHub:
    """行星（10-04）：一个标签或一张人物卡，挂着的记忆够多就单独成一颗星球，点进去只看这些。
    挂着超过一半记忆的那颗是太阳（sun）——通常是用户自己，几乎每条都提到 TA。"""
    key: str                 # "tag:健康" / "person:12"
    kind: str                # "tag" / "person"
    label: str
    members: list[int]       # 挂着的记忆 id（不含人物卡自己）
    node_id: int | None = None   # 人物卡的那颗点；标签没有
    sun: bool = False


@dataclass
class GraphUser:
    """星图正中间的「你」：正文里提到用户名字的记忆都挂在这儿。
    没给名字时，用挂着一半以上记忆的那张人物卡（find_hubs 的 sun）。"""
    label: str
    members: list[int]
    node_id: int | None = None   # 用户自己的人物卡（有的话）——它不再当一颗星星画


@dataclass
class Graph:
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    hubs: list[GraphHub] = field(default_factory=list)
    user: GraphUser | None = None

    def to_dict(self) -> dict:
        return {"nodes": [asdict(n) for n in self.nodes], "edges": [asdict(e) for e in self.edges],
                "hubs": [asdict(h) for h in self.hubs], "user": asdict(self.user) if self.user else None}


def find_hubs(nodes: list[GraphNode], edges: list[GraphEdge], *, min_members: int = 6,
              min_frac: float = 0.05, sun_frac: float = 0.5) -> list[GraphHub]:
    """挂着的记忆 ≥ max(min_members, 总数 × min_frac) 才算行星：记忆少的人、多的人门槛都合适。
    挂着 ≥ 总数 × sun_frac 的人物卡，最大的那张当太阳（最多一颗）。"""
    mem_ids = [n.id for n in nodes if n.kind != "person"]
    total = len(mem_ids)
    need = max(min_members, math.ceil(total * min_frac))
    hubs: list[GraphHub] = []
    by_tag: dict[str, list[int]] = {}
    for n in nodes:
        if n.kind != "person":
            for t in n.tags:
                by_tag.setdefault(t, []).append(n.id)
    for t, ids in by_tag.items():
        if len(ids) >= need:
            hubs.append(GraphHub(key=f"tag:{t}", kind="tag", label=t, members=ids))
    names = {n.id: n.label for n in nodes if n.kind == "person"}
    linked: dict[int, list[int]] = {}
    for e in edges:
        if e.kind == "person" and e.source in names:
            linked.setdefault(e.source, []).append(e.target)
    for pid, ids in linked.items():
        if len(ids) >= need:
            hubs.append(GraphHub(key=f"person:{pid}", kind="person", label=names[pid], members=ids, node_id=pid))
    hubs.sort(key=lambda h: (-len(h.members), h.key))
    top = next((h for h in hubs if h.kind == "person"), None)     # 太阳只能是人，标签再大也不是「你」
    if top and total and len(top.members) >= total * sun_frac:
        top.sun = True
    return hubs


def find_user(nodes: list[GraphNode], hubs: list[GraphHub], contents: dict[int, str],
              user_names=()) -> tuple[GraphUser | None, list[GraphHub]]:
    """找出正中间的「你」，返回 (user, 剩下的行星)。用户自己的人物卡不再当行星。"""
    names = [n.strip() for n in user_names if n and n.strip()]
    if names:
        members = [i for i, text in contents.items() if any(mentions(n, text) >= 0 for n in names)]
        low = {n.lower() for n in names}
        card = next((n.id for n in nodes if n.kind == "person" and n.label.lower() in low), None)
        rest = [replace(h, sun=False) for h in hubs if card is None or h.node_id != card]
        return GraphUser(label=names[0], members=members, node_id=card), rest
    sun = next((h for h in hubs if h.sun), None)
    if sun is None:
        return None, hubs
    return GraphUser(label=sun.label, members=sun.members, node_id=sun.node_id), [h for h in hubs if h is not sun]


async def graph(pool: asyncpg.Pool, user_id: UUID, *, k: int = 3, min_sim: float = 0.6,
                now: datetime | None = None, cfg: MemoryConfig = DEFAULT, user_names=()) -> Graph:
    """user_names：用户的名字（和别名），正文里提到的记忆挂到正中间的「你」上。"""
    now = now or datetime.now(timezone.utc)
    rows = await pool.fetch(
        f"SELECT {MEMORY_COLUMNS}, embedding FROM memories WHERE user_id = $1 AND kind NOT IN ('about', 'diary') ORDER BY id", user_id)
    mems = [row_to_memory(r) for r in rows]
    nodes = [
        GraphNode(id=m.id, label=(m.name or m.title or m.content)[:LABEL_CHARS], kind=m.kind,
                  weight=round(weight(m, now, cfg), 4), title=m.title, tags=list(m.tags))
        for m in mems
    ]
    edges: list[GraphEdge] = []
    seen: set[frozenset] = set()

    with_vec = [(m.id, np.asarray(r["embedding"], dtype=np.float32))
                for m, r in zip(mems, rows) if r["embedding"] is not None and m.kind != "person"]
    if len(with_vec) > 1:
        ids = [i for i, _ in with_vec]
        mat = np.stack([v for _, v in with_vec])
        sim = mat @ mat.T
        np.fill_diagonal(sim, -1.0)
        for a in range(len(ids)):
            for b in np.argsort(-sim[a])[:k]:
                s = float(sim[a, b])
                pair = frozenset((ids[a], ids[int(b)]))
                if s >= min_sim and pair not in seen:
                    seen.add(pair)
                    edges.append(GraphEdge(source=ids[a], target=ids[int(b)], kind="similar",
                                           strength=round(s, 4)))

    for p in (m for m in mems if m.kind == "person"):
        names = keywords(p)
        for m in mems:
            if m.kind == "person":
                continue
            if any(mentions(n, m.content) >= 0 for n in names):
                edges.append(GraphEdge(source=p.id, target=m.id, kind="person", strength=1.0))
    user, hubs = find_user(nodes, find_hubs(nodes, edges),
                           {m.id: m.content for m in mems if m.kind != "person"}, user_names)
    return Graph(nodes=nodes, edges=edges, hubs=hubs, user=user)
