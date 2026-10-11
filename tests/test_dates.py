from datetime import date, datetime, timezone

import pytest

from memory import dates as D
from memory import store
from memory.embed import FakeEmbedder

E = FakeEmbedder()
TODAY = date(2026, 10, 4)


async def test_add_list_update_delete(pool, user_a, user_b):
    a = await D.add_date(pool, user_a, "2026-10-10", "  面试 ", at_time="9:30", note="穿那件灰西装")
    assert a.title == "面试" and a.at_time == "09:30" and a.day == date(2026, 10, 10)
    await D.add_date(pool, user_a, date(2026, 10, 6), "坐飞机")
    assert [d.title for d in await D.list_dates(pool, user_a)] == ["坐飞机", "面试"]
    assert await D.list_dates(pool, user_b) == []
    u = await D.update_date(pool, user_a, a.id, at_time="", title="二面")
    assert u.at_time == "" and u.title == "二面" and u.note == "穿那件灰西装" and u.day == a.day
    assert await D.update_date(pool, user_b, a.id, title="x") is None       # 别人的改不了
    assert await D.delete_date(pool, user_a, a.id) and not await D.delete_date(pool, user_a, a.id)


async def test_bad_input(pool, user_a):
    for kw in ({"day": "10月10日", "title": "x"}, {"day": "2026-10-10", "title": "  "},
               {"day": "2026-10-10", "title": "x", "at_time": "25:00"}):
        with pytest.raises(ValueError):
            await D.add_date(pool, user_a, kw.pop("day"), kw.pop("title"), **kw)


async def test_resolve_with_result_saves_a_memory(pool, user_a):
    d = await D.add_date(pool, user_a, "2026-10-03", "面试")
    r = await D.resolve_date(pool, E, user_a, d.id, result="拿到 offer 了")
    assert r.resolved_at is not None and r.result == "拿到 offer 了"
    assert await D.list_dates(pool, user_a) == []
    assert len(await D.list_dates(pool, user_a, include_resolved=True)) == 1
    mems = await store.list_memories(pool, user_a)
    assert [m.content for m in mems] == ["2026-10-03 面试：拿到 offer 了"] and mems[0].title == "面试"
    back = await D.resolve_date(pool, E, user_a, d.id, resolved=False)
    assert back.resolved_at is None and back.result == "拿到 offer 了"


def _d(i, day, title, at="", resolved=False):
    return D.FarDate(id=i, day=day, at_time=at, title=title, note="", created_at=datetime.now(timezone.utc),
                     resolved_at=datetime.now(timezone.utc) if resolved else None, result="")


def test_upcoming_lines():
    ds = [_d(1, date(2026, 10, 3), "面试"), _d(2, date(2026, 10, 4), "体检", "14:00"),
          _d(3, date(2026, 10, 5), "坐飞机", "07:10"), _d(4, date(2026, 10, 12), "考试"),
          _d(5, date(2026, 10, 30), "太远了"), _d(6, date(2026, 10, 2), "前天的"),
          _d(7, date(2026, 10, 5), "了结过的", resolved=True)]
    assert D.upcoming_lines(ds, TODAY) == [
        "昨天：面试（还没问怎么样）", "今天：体检（14:00）", "明天：坐飞机（07:10）", "还有 8 天：10/12 考试"]
    assert D.upcoming_lines(ds, TODAY, "en")[3] == "In 8 days: Oct 12 考试"


async def test_wipe_and_export_include_dates(pool, user_a):
    await D.add_date(pool, user_a, "2026-10-10", "面试")
    assert [d["title"] for d in (await store.export(pool, user_a))["dates"]] == ["面试"]
    await store.wipe(pool, user_a)
    assert await D.list_dates(pool, user_a, include_resolved=True) == []
