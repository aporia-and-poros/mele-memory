"""联想评测：把编出来的记忆存给一个临时用户，跑几组提问，看搜索和浮现准不准。跑完把临时用户清掉。
每次改 config / 门槛 / 召回链路前后各跑一遍对比。

几组提问（dataset.json）：
- queries：老的 16 句（12 句该想起什么 + 4 句闲聊不该想起）——跟 09-26 的数字可比
- name_queries：句子里带称呼（它的名字 / 昵称 / 用户的名字），看称呼会不会把关键词榜喊坏
- context_queries：代词、「上次」「之前说过」这类要靠前几句才懂的（召回哨兵管的）
- 只读：同一批问法连跑三遍，看浮现有没有改库（recall_count）、结果有没有越跑越偏
- dedup_queries：换了件衣服的同一件事——这一潮已经递过原版，别再递重复的那条

用法：
  .venv/bin/python eval/run_eval.py            # 真 bge-m3
  .venv/bin/python eval/run_eval.py --fake     # 假向量，只验证脚本本身能跑
  .venv/bin/python eval/run_eval.py --rerank   # 细读（本地精排）开着
"""
import argparse
import asyncio
import inspect
import json
import os
import statistics
import time
import uuid
from pathlib import Path

import memory as M
import importlib
msearch = importlib.import_module("memory.search")   # memory 包把 search 这个名字给了函数，模块要这样拿

HERE = Path(__file__).parent


def opt(fn, **kw):
    """只把这版代码认得的参数传进去：改之前那一版也能跑同一份评测。"""
    names = inspect.signature(fn).parameters
    return {k: v for k, v in kw.items() if k in names and v is not None}


def score(got, expect):
    right = sum(1 for g in got if g in expect)
    return right, len(got) - right, sum(1 for e in expect if e not in got)


async def store_all(pool, embedder, user, memories):
    cfg = M.MemoryConfig(merge_threshold=1.01)   # 每条都是独立的一件事，不合并
    ids = {}
    for m in memories:
        r = await M.remember(pool, embedder, user, m["content"], importance=m["importance"], cfg=cfg)
        ids[m["id"]] = r.memory.id
    return ids


async def main(dsn: str, fake: bool, rerank: bool) -> None:
    data = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))
    embedder = M.FakeEmbedder() if fake else M.BgeM3Embedder()
    reranker = M.BgeReranker() if rerank else None
    names = [data["names"]["ai"], *data["names"]["nicknames"], data["names"]["user"]]
    probe_fn = None   # 召回哨兵（先让小模型读懂这句再搜）在 Mele 里接的是大模型，这里不带；M.recall 的 probe 参数可以接你自己的
    await M.apply_schema(dsn)
    pool = await M.create_pool(dsn)
    user, dedup_user = uuid.uuid4(), uuid.uuid4()
    summary = []
    try:
        ids = await store_all(pool, embedder, user, data["memories"])
        back = {v: k for k, v in ids.items()}

        async def recall(q, *, level="medium", context=None, recent=(), shown=(), who=user, idmap=None):
            probe = await probe_fn(q, list(recent)) if probe_fn else None
            rr = reranker
            r = await M.recall(pool, embedder, who, q, level=level, context=context,
                               already_shown=frozenset((idmap or ids)[s] for s in shown),
                               **opt(M.recall, reranker=rr, names=names, probe=probe))
            b = {v: k for k, v in (idmap or ids).items()}
            return [b[m.id] for m in r.full] + [b[l.memory_id] for l in r.lines]

        # ── 老的：搜索 + 三档浮现 ──
        hit5 = total = 0
        for q in data["queries"]:
            if not q["expect"]:
                continue
            total += 1
            hits = await M.search(pool, embedder, user, q["q"], limit=5, **opt(M.search, touch=False, names=names))
            hit5 += any(back.get(h.memory.id) in q["expect"] for h in hits)
        print(f"搜索 前5命中：{hit5}/{total}")
        summary.append(f"搜索前5 {hit5}/{total}")

        for level in ("light", "medium", "rich"):
            right = wrong = missed = noise = 0
            spent = []
            for q in data["queries"]:
                t0 = time.perf_counter()
                got = await recall(q["q"], level=level)
                spent.append((time.perf_counter() - t0) * 1000)
                if not q["expect"]:
                    noise += len(got)
                    continue
                r, w, m = score(got, q["expect"])
                right, wrong, missed = right + r, wrong + w, missed + m
                print(f"  [{level}] {q['q'][:24]:<24} → {got}  期望 {q['expect']}")
            print(f"浮现[{level}]：塞对 {right}，塞错 {wrong}，漏掉 {missed}，闲聊误塞 {noise}；"
                  f"每句 {statistics.median(spent):.0f}ms（中位）")
            summary.append(f"{level} 对{right}/错{wrong}/漏{missed}/噪{noise}/{statistics.median(spent):.0f}ms")

        # ── 称呼 ──
        n_mem = len(data["memories"])
        cover = []
        for q in data["queries"] + data["name_queries"]:
            kw = await msearch.keyword_ids(pool, user, q["q"], 1000, **opt(msearch.keyword_ids, names=names))
            cover.append(len(kw) / n_mem)
        right = wrong = missed = noise = 0
        for q in data["name_queries"]:
            got = await recall(q["q"])
            print(f"  [称呼] {q['q'][:24]:<24} → {got}  期望 {q['expect']}")
            if not q["expect"]:
                noise += len(got)
                continue
            r, w, m = score(got, q["expect"])
            right, wrong, missed = right + r, wrong + w, missed + m
        print(f"称呼：关键词命中全库 中位 {statistics.median(cover):.0%}、最多 {max(cover):.0%}；"
              f"浮现 塞对 {right}，塞错 {wrong}，漏掉 {missed}，闲聊误塞 {noise}")
        summary.append(f"称呼 关键词中位{statistics.median(cover):.0%}/最多{max(cover):.0%} 对{right}/错{wrong}/漏{missed}/噪{noise}")

        # ── 上下文（代词、上次）──
        right = wrong = missed = 0
        for q in data["context_queries"]:
            last_ai = next((l.split("：", 1)[1] for l in reversed(q["recent"]) if l.startswith("Lumi")), None)
            got = await recall(q["q"], context=last_ai, recent=q["recent"])
            print(f"  [上下文] {q['q'][:24]:<24} → {got}  期望 {q['expect']}")
            r, w, m = score(got, q["expect"])
            right, wrong, missed = right + r, wrong + w, missed + m
        print(f"上下文：塞对 {right}，塞错 {wrong}，漏掉 {missed}")
        summary.append(f"上下文 对{right}/错{wrong}/漏{missed}")

        # ── 只读：连跑三遍 ──
        before = await pool.fetchval("SELECT coalesce(sum(recall_count), 0) FROM memories WHERE user_id = $1", user)
        runs = []
        for _ in range(3):
            runs.append([await recall(q["q"]) for q in data["queries"]])
        after = await pool.fetchval("SELECT coalesce(sum(recall_count), 0) FROM memories WHERE user_id = $1", user)
        drift = sum(1 for a, b in zip(runs[0], runs[2]) if a != b)
        print(f"只读：三遍浮现改了库 {after - before} 次，第一遍和第三遍结果不同的句子 {drift} 句")
        summary.append(f"只读 写库{after - before}次/漂移{drift}句")

        # ── 去重 ──
        dids = await store_all(pool, embedder, dedup_user, data["memories"] + data["dedup_memories"])
        dup = kept = 0
        for q in data["dedup_queries"]:
            got = await recall(q["q"], shown=q["shown"], who=dedup_user, idmap=dids)
            print(f"  [去重] {q['q'][:24]:<24} → {got}  已递过 {q['shown']}，不该再递 {q['forbid']}")
            dup += sum(1 for g in got if g in q["forbid"])
            kept += len(got)
        print(f"去重：递了重复的 {dup} 条（共递 {kept} 条）")
        summary.append(f"去重 重复{dup}/共{kept}")
        print("\n汇总：" + "；".join(summary))
    finally:
        await M.wipe(pool, user)
        await M.wipe(pool, dedup_user)
        await pool.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("MEMORY_DSN", "postgresql://localhost/mele_memory_eval"))
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--rerank", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.dsn, args.fake, args.rerank))
