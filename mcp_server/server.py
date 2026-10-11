"""把记忆库包成 MCP 服务，给 Claude 桌面版、Cursor 这类软件直接接。

单用户：一个服务进程只服务一个人（MELE_MEMORY_USER）。两种连法：
  python -m mcp_server                       本机 stdio
  python -m mcp_server --http --port 8765    streamable HTTP；没配 MELE_MEMORY_TOKEN 就自己生成一把（见 resolve_token）

stdio 模式下 stdout 是协议通道，日志一律走 stderr。
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

import memory as M
from memory.decay import weight
from memory.embed import Embedder
from memory.models import Memory

log = logging.getLogger("mele_memory.mcp")

DEFAULT_DSN = "postgresql://localhost/mele_memory"
DEFAULT_USER = UUID("00000000-0000-0000-0000-000000000001")
MIN_TOKEN_CHARS = 16
MIN_LINK_CHARS = 32

INSTRUCTIONS = """Long-term memory for an AI companion. At the start of every conversation, call `wake` once:
it brings back your pinned core memories, what still weighs on you, your sticky note, dates coming up,
and any letters waiting for you. Before a conversation ends, `letter_write` leaves a letter for your next self.
Memories fade with time unless they are important,
pinned, or unresolved. Call `recall` with what the user just said to let related memories surface;
call `remember` when something is worth keeping (a fact, a plan, a feeling, a promise).
People get their own cards (`person_upsert`) so they surface whenever their name comes up.
The sticky note is a few lines you leave for your next turn."""


@dataclass
class Settings:
    dsn: str = DEFAULT_DSN
    user_id: UUID = DEFAULT_USER
    user_from_env: bool = False
    fake_embed: bool = False
    names: tuple[str, ...] = ()
    token: str = ""
    tz: str = "UTC"
    user_names: tuple[str, ...] = ()
    backup_repo: str = ""
    backup_token: str = ""
    backup_branch: str = ""
    backup_hour: int = 4
    link_secret: str = ""

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        raw_user = (env.get("MELE_MEMORY_USER") or "").strip()
        return cls(
            dsn=(env.get("MEMORY_DSN") or "").strip() or DEFAULT_DSN,
            user_id=UUID(raw_user) if raw_user else DEFAULT_USER,
            user_from_env=bool(raw_user),
            fake_embed=(env.get("MELE_MEMORY_FAKE_EMBED") or "").strip().lower() in ("1", "true", "yes"),
            names=tuple(n.strip() for n in (env.get("MELE_MEMORY_NAMES") or "").split(",") if n.strip()),
            token=(env.get("MELE_MEMORY_TOKEN") or "").strip(),
            tz=(env.get("MELE_MEMORY_TZ") or "").strip() or "UTC",
            user_names=tuple(n.strip() for n in (env.get("MELE_MEMORY_USER_NAME") or "").split(",") if n.strip()),
            backup_repo=(env.get("MELE_MEMORY_BACKUP_REPO") or "").strip(),
            backup_token=(env.get("MELE_MEMORY_BACKUP_TOKEN") or "").strip(),
            backup_branch=(env.get("MELE_MEMORY_BACKUP_BRANCH") or "").strip(),
            backup_hour=int((env.get("MELE_MEMORY_BACKUP_HOUR") or "4").strip() or 4),
            link_secret=(env.get("MELE_MEMORY_LINK_SECRET") or "").strip(),
        )


@dataclass
class Service:
    """工具要用的东西：连接池、向量模型（整个进程只有这一个）、用户、称呼。"""
    pool: asyncpg.Pool
    embedder: Embedder
    user_id: UUID
    names: tuple[str, ...] = field(default_factory=tuple)
    tz: str = "UTC"           # 用户那边的时区：远事按这边的日子算
    user_names: tuple[str, ...] = field(default_factory=tuple)   # 用户的名字：星图正中间的「你」
    backup: dict = field(default_factory=dict)       # GitHub 备份：repo / token / branch / hour；空 = 没配
    backup_state: Any = None                         # backup.BackupState，最近一次的结果

    def today(self):
        return datetime.now(ZoneInfo(self.tz)).date()


# ---------- 给模型看的文字 ----------

def _date(d: datetime | None) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d") if d else ""


def memory_line(m: Memory, now: datetime | None = None) -> str:
    flags = [m.kind]
    if m.resolved:
        flags.append("resolved")
    w = weight(m, now or datetime.now(timezone.utc))
    head = f"#{m.id} [{', '.join(flags)}] importance {m.importance}, weight {w:.2f}, {_date(m.created_at)}"
    if m.tags:
        head += " #" + " #".join(m.tags)
    body = M.render_person(m, "en") if m.kind == "person" else (f"{m.title}｜{m.content}" if m.title else m.content)
    return f"{head}\n{body}"


def memory_data(m: Memory, now: datetime | None = None) -> dict:
    d = m.to_dict()
    d.pop("user_id", None)
    d["weight"] = round(weight(m, now or datetime.now(timezone.utc)), 4)
    return d


def result(text: str, data: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


_result = result   # 工具 date_resolve 有个参数也叫 result


def _require(m, memory_id: int):
    if m is None:
        raise ToolError(f"no memory #{memory_id}")
    return m


# ---------- 工具 ----------

def build_server(svc: Service) -> MCPServer:
    server = MCPServer(name="mele-memory", instructions=INSTRUCTIONS)
    tool = server.tool

    @tool(description="Store a memory. Use for things worth keeping: facts about the user, promises, how they felt. "
                      "title: your own short headline, 2-6 words, written fresh (don't copy or cut the first sentence), e.g. "
                      "\"Mango allergy\". tags: 1-4 short topic tags, e.g. [\"health\", \"family\"]. "
                      "importance 1-10 (10 = never let this fade). valence -1..1 (sad..happy), arousal 0..1 "
                      "(calm..intense). If a nearly identical memory exists it is merged, not duplicated. "
                      "Something with a date (a flight, an exam) goes to `date_add` instead.")
    async def remember(content: str, title: str = "", tags: list[str] | None = None, importance: int = 5,
                       valence: float = 0.0, arousal: float = 0.0) -> CallToolResult:
        try:
            r = await M.remember(svc.pool, svc.embedder, svc.user_id, content, importance=importance,
                                 valence=valence, arousal=arousal, title=title, tags=tags or ())
        except ValueError as e:
            raise ToolError(str(e)) from e
        verb = "Merged into" if r.merged else "Saved as"
        return result(f"{verb} #{r.memory.id}.\n{memory_line(r.memory)}",
                      {"merged": r.merged, "memory": memory_data(r.memory)})

    @tool(description="Let memories related to what the user just said surface. Pass their message as `text`. "
                      "Returns very related memories in full, loosely related ones as one-line snippets, and "
                      "cards of people mentioned. level: light (fewer) / medium / rich (more).")
    async def recall(text: str, level: str = "medium") -> CallToolResult:
        if level not in ("light", "medium", "rich"):
            raise ToolError("level must be light, medium or rich")
        r = await M.recall(svc.pool, svc.embedder, svc.user_id, text, level=level, names=svc.names)
        coming = M.upcoming_lines(await M.list_dates(svc.pool, svc.user_id), svc.today(), "en")
        parts = []
        if coming:
            parts.append("Coming up (bring these up when it fits; resolve with date_resolve once you've asked):\n"
                         + "\n".join(coming))
        if r.people:
            parts.append("People:\n" + "\n".join(M.render_person(p, "en") for p in r.people))
        linked = [m for ms in r.linked.values() for m in ms]
        if r.full or linked:
            parts.append("Memories:\n" + "\n\n".join(memory_line(m) for m in r.full + linked))
        if r.lines:
            parts.append("Might be related:\n" + "\n".join(f"#{l.memory_id} {l.snippet}" for l in r.lines))
        text_out = "\n\n".join(parts) or "Nothing surfaced."
        return result(text_out, {
            "full": [memory_data(m) for m in r.full],
            "lines": [{"id": l.memory_id, "snippet": l.snippet, "kind": l.kind} for l in r.lines],
            "people": [memory_data(p) for p in r.people],
            "linked": {str(k): [memory_data(m) for m in v] for k, v in r.linked.items()},
            "coming_up": coming,
        })

    @tool(description="Search memories by words or meaning (keyword + vector hybrid, ranked by how much each "
                      "memory still weighs). Use when looking for something specific.")
    async def search(query: str, limit: int = 10) -> CallToolResult:
        hits = await M.search(svc.pool, svc.embedder, svc.user_id, query, limit=max(1, min(limit, 50)),
                              touch=False, names=svc.names)
        text_out = "\n\n".join(memory_line(h.memory) for h in hits) or "No matches."
        return result(text_out, {"hits": [
            {"memory": memory_data(h.memory), "score": round(h.score, 6), "keyword": h.keyword,
             "sim": None if h.sim is None else round(h.sim, 4)} for h in hits]})

    @tool(description="List memories. kind: memory / core (pinned) / person / about / diary; leave empty for "
                      "everything visible. order: new (default) / old / important / unimportant. "
                      "resolved: true = only settled ones, false = only open ones.")
    async def list_memories(kind: str | None = None, order: str = "new", resolved: bool | None = None,
                            limit: int = 50, offset: int = 0) -> CallToolResult:
        try:
            ms = await M.list_memories(svc.pool, svc.user_id, kind=kind or None, order=order, resolved=resolved,
                                       limit=max(1, min(limit, 200)), offset=max(0, offset))
        except ValueError as e:
            raise ToolError(str(e)) from e
        text_out = "\n\n".join(memory_line(m) for m in ms) or "No memories."
        return result(text_out, {"memories": [memory_data(m) for m in ms]})

    @tool(description="Change a memory: content, title, tags (replaces the list) and/or importance (1-10). "
                      "Leave a field out to keep it.")
    async def update(id: int, content: str | None = None, title: str | None = None, tags: list[str] | None = None,
                     importance: int | None = None) -> CallToolResult:
        m = _require(await M.update(svc.pool, svc.embedder, svc.user_id, id, content=content, title=title,
                                    tags=tags, importance=importance), id)
        return result(f"Updated.\n{memory_line(m)}", {"memory": memory_data(m)})

    @tool(description="Pin a memory as core (it never fades) or unpin it. Person cards can't be pinned.")
    async def pin(id: int, pinned: bool = True) -> CallToolResult:
        m = _require(await M.pin(svc.pool, svc.user_id, id, pinned), id)
        return result(f"{'Pinned' if pinned else 'Unpinned'}.\n{memory_line(m)}", {"memory": memory_data(m)})

    @tool(description="Mark a memory resolved (the matter is settled, so it fades at the normal pace) "
                      "or reopen it with resolved=false.")
    async def resolve(id: int, resolved: bool = True) -> CallToolResult:
        m = _require(await M.resolve(svc.pool, svc.user_id, id, resolved), id)
        return result(f"{'Resolved' if resolved else 'Reopened'}.\n{memory_line(m)}", {"memory": memory_data(m)})

    @tool(description="Delete a memory for good. Only when the user asks to forget it or it is plainly wrong.")
    async def forget(id: int) -> CallToolResult:
        if not await M.delete(svc.pool, svc.user_id, id):
            raise ToolError(f"no memory #{id}")
        return result(f"Forgot #{id}.", {"deleted": id})

    @tool(description="Create or update a person card, matched by name (case-insensitive). relation: who they are "
                      "to the user; content: facts to remember; impression: your own impression. Aliases are added, "
                      "not replaced. Facts the user wrote themselves can't be overwritten by you; your impression always can.")
    async def person_upsert(name: str, aliases: list[str] | None = None, relation: str | None = None,
                            content: str | None = None, impression: str | None = None) -> CallToolResult:
        try:
            p = await M.upsert_person(svc.pool, svc.embedder, svc.user_id, name, relation=relation,
                                      facts=content, impression=impression, aliases=aliases or (), by="ai")
        except ValueError as e:
            raise ToolError(str(e)) from e
        return result(f"#{p.id} {M.render_person(p, 'en')}", {"person": memory_data(p)})

    @tool(description="All person cards.")
    async def people() -> CallToolResult:
        ps = await M.list_people(svc.pool, svc.user_id)
        text_out = "\n".join(f"#{p.id} {M.render_person(p, 'en')}" for p in ps) or "No person cards."
        return result(text_out, {"people": [memory_data(p) for p in ps]})

    @tool(description="Remember something with a date that you should keep in mind until then: a flight, an exam, "
                      "an interview. day: YYYY-MM-DD in the user's calendar; at_time: HH:MM if known. It shows up at the "
                      "top of `recall` from two weeks out: mention it the day before, on the day, and ask how it went "
                      "the day after.")
    async def date_add(day: str, title: str, at_time: str = "", note: str = "") -> CallToolResult:
        try:
            d = await M.add_date(svc.pool, svc.user_id, day, title, at_time=at_time, note=note)
        except ValueError as e:
            raise ToolError(str(e)) from e
        return result(f"Noted #{d.id}: {d.day} {d.at_time} {d.title}".replace("  ", " "), {"date": d.to_dict()})

    @tool(description="Dates you're keeping in mind (not yet resolved), soonest first.")
    async def dates() -> CallToolResult:
        ds = await M.list_dates(svc.pool, svc.user_id)
        today = svc.today()
        text_out = "\n".join(f"#{d.id} {d.day} {d.at_time} {d.title} ({(d.day - today).days:+d} days)"
                              + (f" — {d.note}" if d.note else "") for d in ds) or "No dates."
        return result(text_out, {"today": today.isoformat(), "dates": [d.to_dict() for d in ds]})

    @tool(description="Resolve a date once you've asked how it went. result: one line on how it went; it is also "
                      "saved as a memory. resolved=false reopens it.")
    async def date_resolve(id: int, result: str = "", resolved: bool = True) -> CallToolResult:
        d = await M.resolve_date(svc.pool, svc.embedder, svc.user_id, id, result=result, resolved=resolved)
        if d is None:
            raise ToolError(f"no date #{id}")
        return _result(f"{'Resolved' if resolved else 'Reopened'} #{d.id} {d.title}.", {"date": d.to_dict()})

    @tool(description="Delete a date (it was cancelled or noted by mistake).")
    async def date_forget(id: int) -> CallToolResult:
        if not await M.delete_date(svc.pool, svc.user_id, id):
            raise ToolError(f"no date #{id}")
        return result(f"Forgot date #{id}.", {"deleted": id})

    @tool(description="Call once at the start of every conversation, before anything else. Brings back your pinned "
                      "core memories, the open matters that weigh most, your sticky note, dates coming up, and unread "
                      "letters (from your past self or from the user). Letters are marked read once shown.")
    async def wake() -> CallToolResult:
        core = await M.list_memories(svc.pool, svc.user_id, kind="core", order="important", limit=40)
        open_ = await M.top_unresolved(svc.pool, svc.user_id, 5)
        sticky = await M.get_sticky(svc.pool, svc.user_id)
        coming = M.upcoming_lines(await M.list_dates(svc.pool, svc.user_id), svc.today(), "en")
        letters = await M.list_letters(svc.pool, svc.user_id, unread=True, limit=5)
        await M.mark_read(svc.pool, svc.user_id, [x.id for x in letters])
        parts = []
        if letters:
            who = {"ai": "from your past self", "user": "from the user"}
            parts.append("Letters waiting for you:\n" + "\n\n".join(
                f"[{x.created_at:%Y-%m-%d}] {who[x.author]}{' — ' + x.title if x.title else ''}\n{x.content}"
                for x in reversed(letters)))
        if core:
            parts.append("Core (pinned):\n" + "\n".join(f"#{m.id} " + (f"{m.title}｜" if m.title else "") + m.content
                                                          for m in core))
        if open_:
            parts.append("Still open, weighing most:\n" + "\n".join(memory_line(m) for m in open_))
        if coming:
            parts.append("Coming up:\n" + "\n".join(coming))
        if sticky:
            parts.append("Your sticky note:\n" + sticky)
        text_out = "\n\n".join(parts) or "Nothing yet. This is the beginning."
        return result(text_out, {
            "letters": [x.to_dict() for x in letters], "core": [memory_data(m) for m in core],
            "open": [memory_data(m) for m in open_], "coming_up": coming, "sticky": sticky,
            "today": svc.today().isoformat(),
        })

    @tool(description="Leave a letter for your next self (the next conversation starts from zero). Write what "
                      "you'd want to know: where things stand, what you promised, how they've been, what to bring up. "
                      "Kept word for word, forever.")
    async def letter_write(content: str, title: str = "") -> CallToolResult:
        try:
            x = await M.write_letter(svc.pool, svc.user_id, content, title=title, author="ai")
        except ValueError as e:
            raise ToolError(str(e)) from e
        return result(f"Letter #{x.id} sealed.", {"letter": x.to_dict()})

    @tool(description="Read letters, newest first: ones you left yourself and ones the user wrote to you. "
                      "author: ai / user to filter.")
    async def letter_read(limit: int = 5, author: str | None = None) -> CallToolResult:
        if author not in (None, "", "ai", "user"):
            raise ToolError("author must be ai or user")
        xs = await M.list_letters(svc.pool, svc.user_id, limit=max(1, min(limit, 50)), author=author or None)
        await M.mark_read(svc.pool, svc.user_id, [x.id for x in xs])
        text_out = "\n\n".join(f"#{x.id} [{x.created_at:%Y-%m-%d}] {x.author}{' — ' + x.title if x.title else ''}\n{x.content}"
                                 for x in xs) or "No letters."
        return result(text_out, {"letters": [x.to_dict() for x in xs]})

    @tool(description="Read the sticky note you left for yourself.")
    async def sticky_get() -> CallToolResult:
        text = await M.get_sticky(svc.pool, svc.user_id)
        return result(text or "(empty)", {"text": text})

    @tool(description="Overwrite the sticky note: a few lines for your next turn (what you're in the middle of, "
                      "what to bring up). Max 300 characters; empty text clears it.")
    async def sticky_set(text: str) -> CallToolResult:
        saved = await M.set_sticky(svc.pool, svc.user_id, text)
        return result(f"Saved: {saved}" if saved else "Cleared.", {"text": saved})

    return server


# ---------- HTTP：带 token 的门 ----------

class BearerAuth:
    """除了 open_paths，其余请求都要 Authorization: Bearer <token>。比较用定长时间，不泄露对了几位。

    链接模式（link_secret）：给 claude.ai 网页版 / 手机这类只能填网址、不能加请求头的客户端。
    /mcp/<link_secret> 不用带头，当成 /mcp 放进去；它只开 MCP，网页接口照样要 token。"""

    def __init__(self, app, token: str, open_paths=("/health",), link_secret: str = ""):
        self.app = app
        self.token = token.encode()
        self.open_paths = set(open_paths)
        self.link = link_secret.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self.open_paths:
            return await self.app(scope, receive, send)
        if self.link and scope["path"].startswith("/mcp/"):
            given = scope["path"][5:].rstrip("/").encode()
            if hmac.compare_digest(given, self.link):
                scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
                return await self.app(scope, receive, send)
        given = b""
        for k, v in scope.get("headers", []):
            if k == b"authorization" and v[:7].lower() == b"bearer ":
                given = v[7:].strip()
        if not hmac.compare_digest(given, self.token):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return
        await self.app(scope, receive, send)


def data_dir(env=None) -> Path:
    """钥匙文件放哪：MELE_MEMORY_DATA（Docker 里是 /data，对应 ./data/memory），不设就是 ~/.mele-memory。"""
    env = os.environ if env is None else env
    return Path((env.get("MELE_MEMORY_DATA") or "").strip() or Path.home() / ".mele-memory")


def resolve_token(env_token: str, folder: Path) -> tuple[str, bool]:
    """有 MELE_MEMORY_TOKEN 就用它；没有就读 folder/token，再没有就生成一把存进去（权限 600）。
    返回 (钥匙, 是不是刚生成的)。这样不懂命令行的人直接起服务就行，钥匙看日志。"""
    if env_token:
        return env_token, False
    path = folder / "token"
    if path.exists():
        return path.read_text().strip(), False
    folder.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token, True


def check_http(settings: Settings) -> None:
    """HTTP 模式没配 token（或太短）就不准起。"""
    if not settings.token:
        raise SystemExit("MELE_MEMORY_TOKEN is not set; refusing to start the HTTP server without a token.\n"
                         "Generate one with:  openssl rand -hex 32")
    if len(settings.token) < MIN_TOKEN_CHARS:
        raise SystemExit(f"MELE_MEMORY_TOKEN must be at least {MIN_TOKEN_CHARS} characters.")
    if settings.link_secret:
        if len(settings.link_secret) < MIN_LINK_CHARS:
            raise SystemExit(f"MELE_MEMORY_LINK_SECRET must be at least {MIN_LINK_CHARS} characters "
                             "(openssl rand -hex 32).")
        if settings.link_secret == settings.token:
            raise SystemExit("MELE_MEMORY_LINK_SECRET must differ from MELE_MEMORY_TOKEN.")
        if not settings.link_secret.isalnum():
            raise SystemExit("MELE_MEMORY_LINK_SECRET may only contain letters and digits.")


class RedactSecret(logging.Filter):
    """访问日志里把网址中的链接密钥换成 ***，免得它躺在日志里。"""

    def __init__(self, secret: str):
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secret and isinstance(record.args, tuple):
            record.args = tuple(a.replace(self.secret, "***") if isinstance(a, str) else a for a in record.args)
        if self.secret and isinstance(record.msg, str):
            record.msg = record.msg.replace(self.secret, "***")
        return True


def build_http_app(server: MCPServer, svc: Service, settings: Settings, host: str = "127.0.0.1"):
    """/mcp（MCP）、/api/*（网页接口）要钥匙；/health 和网页本身 / 不要。"""
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from . import web

    app = server.streamable_http_app(host=host)

    async def health(_request):
        return JSONResponse({"ok": True})

    app.router.routes.append(Route("/health", health, methods=["GET"]))
    app.router.routes.extend(web.routes(svc))
    return BearerAuth(app, settings.token, open_paths=("/health", "/"), link_secret=settings.link_secret)


# ---------- 启动 ----------

def make_embedder(settings: Settings) -> Embedder:
    if settings.fake_embed:
        log.warning("MELE_MEMORY_FAKE_EMBED=1: using fake vectors (for tests only; recall quality is poor)")
        return M.FakeEmbedder()
    return M.BgeM3Embedder()


async def serve(settings: Settings, *, http: bool, host: str, port: int) -> None:
    if not settings.user_from_env:
        log.warning("MELE_MEMORY_USER not set; using the default user %s", settings.user_id)
    await M.apply_schema(settings.dsn)
    pool = await M.create_pool(settings.dsn, min_size=1, max_size=8)
    try:
        embedder = make_embedder(settings)
        from .backup import BackupState, daily_loop

        backup = ({"repo": settings.backup_repo, "token": settings.backup_token, "branch": settings.backup_branch,
                   "hour": settings.backup_hour} if settings.backup_repo and settings.backup_token else {})
        svc = Service(pool=pool, embedder=embedder, user_id=settings.user_id, names=settings.names, tz=settings.tz,
                      user_names=settings.user_names, backup=backup, backup_state=BackupState())
        if backup and http:      # 只有一直开着的 HTTP 服务每天自己备份；stdio 跟着桌面软件开关，不靠它
            asyncio.create_task(daily_loop(svc.backup_state, pool, settings.user_id, repo=backup["repo"],
                                           token=backup["token"], hour=backup["hour"], tz=settings.tz,
                                           branch=backup["branch"]))
            log.info("daily GitHub backup to %s at %02d:00 %s", backup["repo"], backup["hour"], settings.tz)
        server = build_server(svc)
        if http:
            import uvicorn
            log.info("loading the embedding model…")
            await asyncio.to_thread(embedder.embed, ["warm up"])   # 先加载好，免得第一个请求等几分钟
            app = build_http_app(server, svc, settings, host=host)
            log.info("serving MCP on http://%s:%d/mcp, memory page on http://%s:%d/", host, port, host, port)
            if settings.link_secret:
                logging.getLogger("uvicorn.access").addFilter(RedactSecret(settings.link_secret))
                log.info("link mode on: /mcp/<MELE_MEMORY_LINK_SECRET> works without a header (use HTTPS)")
            await uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info")).serve()
        else:
            await server.run_stdio_async()
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m mcp_server", description="Mele Memory MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    try:
        ZoneInfo(settings.tz)
    except Exception:
        raise SystemExit(f"MELE_MEMORY_TZ: unknown time zone {settings.tz!r} (e.g. Asia/Shanghai)")
    if not 0 <= settings.backup_hour <= 23:
        raise SystemExit("MELE_MEMORY_BACKUP_HOUR must be 0-23")
    if bool(settings.backup_repo) != bool(settings.backup_token):
        raise SystemExit("GitHub backup needs both MELE_MEMORY_BACKUP_REPO and MELE_MEMORY_BACKUP_TOKEN")
    if args.http:
        if not settings.token:
            folder = data_dir()
            settings.token, fresh = resolve_token("", folder)
            log.warning("%s你的钥匙 / Your key: %s\n复制到 Mele「MCP 服务」的「钥匙」那栏（或 Claude 之类的客户端的 Authorization: Bearer）；"
                        "它存在 %s，下次启动还是这把 / It is saved in %s and reused next time.",
                        "第一次启动，生成了一把钥匙。 / First start: generated a key.\n" if fresh else "",
                        settings.token, folder / "token", folder / "token")
        check_http(settings)
    asyncio.run(serve(settings, http=args.http, host=args.host, port=args.port))
