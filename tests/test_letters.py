import pytest

from memory import letters as L
from memory import store
from memory.embed import FakeEmbedder

E = FakeEmbedder()


async def test_write_list_read(pool, user_a, user_b):
    a = await L.write_letter(pool, user_a, "  明天她要考试，记得一开口就问  ", title="给明天的我")
    b = await L.write_letter(pool, user_a, "考完了，我请你吃蛋糕", author="user")
    assert a.content == "明天她要考试，记得一开口就问" and a.author == "ai" and a.read_at is None
    assert [x.id for x in await L.list_letters(pool, user_a)] == [b.id, a.id]
    assert [x.id for x in await L.list_letters(pool, user_a, author="user")] == [b.id]
    assert await L.list_letters(pool, user_b) == []
    await L.mark_read(pool, user_a, [a.id])
    assert [x.id for x in await L.list_letters(pool, user_a, unread=True)] == [b.id]
    await L.mark_read(pool, user_b, [b.id])                      # 别人的标不了
    assert len(await L.list_letters(pool, user_a, unread=True)) == 1
    assert await L.delete_letter(pool, user_a, a.id) and not await L.delete_letter(pool, user_a, a.id)


async def test_bad_letters(pool, user_a):
    with pytest.raises(ValueError):
        await L.write_letter(pool, user_a, "   ")
    with pytest.raises(ValueError):
        await L.write_letter(pool, user_a, "hi", author="someone")


async def test_export_and_wipe_include_letters(pool, user_a):
    await L.write_letter(pool, user_a, "信")
    assert [x["content"] for x in (await store.export(pool, user_a))["letters"]] == ["信"]
    await store.wipe(pool, user_a)
    assert await L.list_letters(pool, user_a) == []


async def test_order_and_resolved_filter(pool, user_a):
    from datetime import datetime, timedelta, timezone
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    a = (await store.remember(pool, E, user_a, "周一去游泳", importance=3, now=t)).memory
    b = (await store.remember(pool, E, user_a, "芒果过敏", importance=9, now=t + timedelta(days=1))).memory
    c = (await store.remember(pool, E, user_a, "下周交论文", importance=6, now=t + timedelta(days=2))).memory
    await store.resolve(pool, user_a, c.id)
    ids = lambda ms: [m.id for m in ms]
    assert ids(await store.list_memories(pool, user_a)) == [c.id, b.id, a.id]
    assert ids(await store.list_memories(pool, user_a, order="old")) == [a.id, b.id, c.id]
    assert ids(await store.list_memories(pool, user_a, order="important")) == [b.id, c.id, a.id]
    assert ids(await store.list_memories(pool, user_a, order="unimportant")) == [a.id, c.id, b.id]
    assert ids(await store.list_memories(pool, user_a, resolved=True)) == [c.id]
    assert ids(await store.list_memories(pool, user_a, resolved=False)) == [b.id, a.id]
    with pytest.raises(ValueError):
        await store.list_memories(pool, user_a, order="random")
    top = await store.top_unresolved(pool, user_a, 2, now=t + timedelta(days=3))
    assert ids(top) == [b.id, a.id]
    st = await store.stats(pool, user_a)
    assert st["total"] == 3 and st["resolved"] == 1 and st["kinds"]["memory"] == 3 and st["no_vector"] == 0
