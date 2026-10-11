"""远事：有日子、还远、要一路惦记着的事（坐飞机、考试、面试）。

它记着，用户看得见、能改能删。离得近了就在联想结果最上面带一行
（「明天：面试（10:00）」「昨天：面试（还没问怎么样）」），模型看到了自己会提——
前一天惦记、当天问候、第二天问结果。问过了就了结：可以顺手留一句结果，存成一条普通记忆。

日期都是用户那边的日子（调用方按用户时区算好 today 传进来）。"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from uuid import UUID

import asyncpg

from .config import DEFAULT, MemoryConfig
from .embed import Embedder

NEAR_DAYS = 14          # 两周以内的才带进联想
MAX_LINES = 5
TITLE_MAX, NOTE_MAX, RESULT_MAX = 60, 200, 300
_HM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

COLUMNS = "id, day, at_time, title, note, created_at, resolved_at, result"


@dataclass
class FarDate:
    id: int
    day: date
    at_time: str            # 'HH:MM' 或空
    title: str
    note: str
    created_at: datetime
    resolved_at: datetime | None
    result: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["day"] = self.day.isoformat()
        d["created_at"] = self.created_at.isoformat()
        d["resolved_at"] = self.resolved_at.isoformat() if self.resolved_at else None
        return d


def _row(r) -> FarDate:
    return FarDate(**{k: r[k] for k in ("id", "day", "at_time", "title", "note", "created_at", "resolved_at", "result")})


def parse_day(v) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip())
    except ValueError as e:
        raise ValueError(f"day must look like 2026-10-10, got {v!r}") from e


def clean_time(v: str | None) -> str:
    v = (v or "").strip()
    if not v:
        return ""
    m = _HM.match(v)
    if not m:
        raise ValueError(f"at_time must look like 09:30, got {v!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _title(v: str | None) -> str:
    t = " ".join((v or "").split())[:TITLE_MAX]
    if not t:
        raise ValueError("title is empty")
    return t


async def add_date(pool: asyncpg.Pool, user_id: UUID, day, title: str, *, at_time: str = "",
                   note: str = "") -> FarDate:
    row = await pool.fetchrow(
        f"""INSERT INTO far_dates (user_id, day, at_time, title, note) VALUES ($1, $2, $3, $4, $5)
            RETURNING {COLUMNS}""",
        user_id, parse_day(day), clean_time(at_time), _title(title), (note or "").strip()[:NOTE_MAX])
    return _row(row)


async def get_date(pool: asyncpg.Pool, user_id: UUID, date_id: int) -> FarDate | None:
    row = await pool.fetchrow(f"SELECT {COLUMNS} FROM far_dates WHERE user_id = $1 AND id = $2", user_id, date_id)
    return _row(row) if row else None


async def update_date(pool: asyncpg.Pool, user_id: UUID, date_id: int, *, day=None, at_time: str | None = None,
                      title: str | None = None, note: str | None = None) -> FarDate | None:
    """给了的字段换掉，没给（None）的不动。"""
    cur = await get_date(pool, user_id, date_id)
    if cur is None:
        return None
    row = await pool.fetchrow(
        f"""UPDATE far_dates SET day = $3, at_time = $4, title = $5, note = $6
            WHERE user_id = $1 AND id = $2 RETURNING {COLUMNS}""",
        user_id, date_id,
        parse_day(day) if day is not None else cur.day,
        clean_time(at_time) if at_time is not None else cur.at_time,
        _title(title) if title is not None else cur.title,
        (note or "").strip()[:NOTE_MAX] if note is not None else cur.note)
    return _row(row)


async def resolve_date(pool: asyncpg.Pool, embedder: Embedder, user_id: UUID, date_id: int, *,
                       result: str = "", resolved: bool = True, now: datetime | None = None,
                       cfg: MemoryConfig = DEFAULT) -> FarDate | None:
    """了结（问过怎么样了）。带了 result 就顺手存一条普通记忆：「2026-10-10 面试：拿到 offer 了」。
    resolved=False 是重开。"""
    from .store import remember

    now = now or datetime.now(timezone.utc)
    result = (result or "").strip()[:RESULT_MAX]
    row = await pool.fetchrow(
        f"""UPDATE far_dates SET resolved_at = $3, result = CASE WHEN $4 = '' THEN result ELSE $4 END
            WHERE user_id = $1 AND id = $2 RETURNING {COLUMNS}""",
        user_id, date_id, now if resolved else None, result)
    if row is None:
        return None
    d = _row(row)
    if resolved and result:
        await remember(pool, embedder, user_id, f"{d.day.isoformat()} {d.title}：{result}", importance=6,
                       title=d.title, now=now, cfg=cfg)
    return d


async def delete_date(pool: asyncpg.Pool, user_id: UUID, date_id: int) -> bool:
    status = await pool.execute("DELETE FROM far_dates WHERE user_id = $1 AND id = $2", user_id, date_id)
    return status.endswith(" 1")


async def list_dates(pool: asyncpg.Pool, user_id: UUID, *, include_resolved: bool = False) -> list[FarDate]:
    rows = await pool.fetch(
        f"""SELECT {COLUMNS} FROM far_dates WHERE user_id = $1 AND ($2 OR resolved_at IS NULL)
            ORDER BY day, at_time, id""", user_id, include_resolved)
    return [_row(r) for r in rows]


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LINE = {
    "zh": {"yesterday": "昨天：{t}（还没问怎么样）", "today": "今天：{t}{at}", "tomorrow": "明天：{t}{at}",
           "later": "还有 {n} 天：{d} {t}{at}", "at": "（{a}）"},
    "en": {"yesterday": "Yesterday: {t} (haven't asked how it went)", "today": "Today: {t}{at}",
           "tomorrow": "Tomorrow: {t}{at}", "later": "In {n} days: {d} {t}{at}", "at": " ({a})"},
}


def upcoming_lines(dates: list[FarDate], today: date, lang: str = "zh") -> list[str]:
    """联想结果最上面那几行：昨天（没了结的）、今天、明天、两周以内。了结过的、更早的不提。"""
    L = _LINE.get(lang, _LINE["zh"])
    out = []
    for d in sorted(dates, key=lambda x: (x.day, x.at_time)):
        if d.resolved_at is not None:
            continue
        n = (d.day - today).days
        at = L["at"].format(a=d.at_time) if d.at_time else ""
        if n == -1:
            out.append(L["yesterday"].format(t=d.title))
        elif n == 0:
            out.append(L["today"].format(t=d.title, at=at))
        elif n == 1:
            out.append(L["tomorrow"].format(t=d.title, at=at))
        elif 1 < n <= NEAR_DAYS:
            label = f"{d.day.month}/{d.day.day}" if lang == "zh" else f"{_MONTHS[d.day.month - 1]} {d.day.day}"
            out.append(L["later"].format(n=n, d=label, t=d.title, at=at))
    return out[:MAX_LINES]
