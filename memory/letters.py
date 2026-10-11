"""信：给下一个窗口的自己留的信，也可以是用户写给它的。

官方端（Claude 桌面版这类）每开一个新对话都是从零开始，上一个窗口想说的话写成一封信，
下一个窗口醒来（wake）时读到。信原文永久保留，不淡、不合并、不改；读过的标上 read_at。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from uuid import UUID

import asyncpg

AUTHORS = ("ai", "user")
TITLE_MAX, CONTENT_MAX = 80, 8000
COLUMNS = "id, author, title, content, created_at, read_at"


@dataclass
class Letter:
    id: int
    author: str          # ai = 它写给以后的自己 / user = 用户写给它
    title: str
    content: str
    created_at: datetime
    read_at: datetime | None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["read_at"] = self.read_at.isoformat() if self.read_at else None
        return d


def _row(r) -> Letter:
    return Letter(**{k: r[k] for k in ("id", "author", "title", "content", "created_at", "read_at")})


async def write_letter(pool: asyncpg.Pool, user_id: UUID, content: str, *, title: str = "", author: str = "ai",
                       now: datetime | None = None) -> Letter:
    if author not in AUTHORS:
        raise ValueError(f"author must be ai or user, got {author!r}")
    content = (content or "").strip()[:CONTENT_MAX]
    if not content:
        raise ValueError("content is empty")
    row = await pool.fetchrow(
        f"""INSERT INTO letters (user_id, author, title, content, created_at) VALUES ($1, $2, $3, $4, $5)
            RETURNING {COLUMNS}""",
        user_id, author, " ".join((title or "").split())[:TITLE_MAX], content, now or datetime.now(timezone.utc))
    return _row(row)


async def list_letters(pool: asyncpg.Pool, user_id: UUID, *, limit: int = 20, offset: int = 0,
                       author: str | None = None, unread: bool = False) -> list[Letter]:
    """新的在前。unread=True 只要没读过的。"""
    rows = await pool.fetch(
        f"""SELECT {COLUMNS} FROM letters WHERE user_id = $1 AND ($2::text IS NULL OR author = $2)
              AND (NOT $3 OR read_at IS NULL)
            ORDER BY created_at DESC, id DESC LIMIT $4 OFFSET $5""",
        user_id, author, unread, limit, offset)
    return [_row(r) for r in rows]


async def mark_read(pool: asyncpg.Pool, user_id: UUID, ids, *, now: datetime | None = None) -> None:
    ids = [int(i) for i in ids]
    if ids:
        await pool.execute("UPDATE letters SET read_at = $3 WHERE user_id = $1 AND id = ANY($2::bigint[]) AND read_at IS NULL",
                           user_id, ids, now or datetime.now(timezone.utc))


async def delete_letter(pool: asyncpg.Pool, user_id: UUID, letter_id: int) -> bool:
    status = await pool.execute("DELETE FROM letters WHERE user_id = $1 AND id = $2", user_id, letter_id)
    return status.endswith(" 1")
