# Mele Memory

给 AI 伴侣用的长期记忆，从 Mele 里单独拿出来，源码公开。

它会记住你说过的事，记得的轻重会随时间淡去；你说一句话，相关的记忆会自己浮上来。

[English below](#english)

## 三种用法，挑一种

| 你想…… | 用这个 | 跳到 |
|---|---|---|
| 一条命令跑起来，带网页、星图、每天备份，能从别的设备连 | Docker | [一键启动](#一键启动docker) |
| 在自己电脑上接 Claude 桌面版 / Cursor，不装 Docker | 本机 MCP | [接到 Claude 桌面版](#接到-claude-桌面版) |
| 在 claude.ai 网页版或手机 App 里用 | Docker + 链接模式 | [接到 claude.ai](#接到-claudeai-网页版--手机) |
| 写自己的 App，把它当库用 | Python 包 | [当库用](#当库用) |

## 它能做什么

**记忆本身**

- **记住**：每条记忆有标题、标签、重要性、情绪。跟已有的很像就合并成一条，不会越存越乱。
- **会淡**：分量 = 重要性 × 时间衰减 × 情绪加成。越要紧的淡得越慢，没了结的事再慢一倍；钉住的核心、人物卡不会淡。被想起不加分，免得常聊的那件事越滚越大，别的要紧事反而沉底。
- **会浮上来（联想）**：你每说一句，拿这句跟你**所有**记忆比，换成标准分（z）：「这条比你平时的记忆像多少个标准差」。所以记忆多的人、少的人、说中文还是英文，门槛都稳。分三档：整条给、给一行摘要、不给。
- **混合搜索**：向量一路 + 关键词一路，按排名融合（RRF），再乘上当前分量。
- **称呼停用词**：它的名字、你给它起的昵称、你自己的名字几乎每条记忆都有，会把关键词搜索「喊坏」。这里按每个用户自动找出太常见的词停掉。
- **不重复递**：同一段对话里递过的，和它意思几乎一样的「双胞胎」也不再递。
- **人物卡**：名字、别名、是谁、要记得的事、它的印象。话里一出现名字就把卡递过去；用户写的事实它改不了。
- **远事**：有日子、要惦记的事（考试、面试、坐飞机）。两周以内的会出现在联想结果最上面，前一天、当天、第二天它会自己提。
- **信**：每个新对话都从零开始，它可以给下一个窗口的自己留一封信；你也可以写信给它。原文永久保留。
- **便利贴**：它给下一轮自己留的几行字。
- **潮汐（`tide/`）**：对话长到一定程度就「卷」：最近 40 条（或 1 万字）原文留着，更早的按天写进一本滚动账本，远的模糊、近的清楚（今天 2500 字、昨天 1000、前天 400、更早一两句）。写账的模型看不到旧账，没东西可抄；每一段都逐句回查原文，编出来的、太长的、用第三人称写自己的，一律打回换模型。另留几句它自己的原话当腔调样本，卷完说话的调子接得住。模型由你接。

**给模型的工具（MCP）**

| 工具 | 什么时候用 |
|---|---|
| `wake` | 每个新对话开头调一次：钉住的核心、最要紧的没了结的事、便利贴、快到的远事、没读的信 |
| `recall` | 拿用户刚说的话，让相关的记忆、人物卡、快到的远事浮上来 |
| `remember` | 存一条（模型自己起标题、配标签，不另外调模型） |
| `search` / `list_memories` | 搜；按日期或重要度列，可以只看了结的 |
| `update` / `pin` / `resolve` / `forget` | 改、钉住、了结、删 |
| `person_upsert` / `people` | 人物卡 |
| `date_add` / `dates` / `date_resolve` / `date_forget` | 远事 |
| `letter_write` / `letter_read` | 给下一个窗口的自己留信、读信 |
| `sticky_get` / `sticky_set` | 便利贴 |

潮汐不在工具里：它要整段聊天记录，那在聊天软件手里。

**网页**（HTTP 模式自带）：记忆列表、记忆星图、人物卡、信、状态。见[网页](#网页)。

## 一键启动（Docker）

```bash
docker compose up -d
docker compose logs memory | grep -A1 钥匙   # 第一次启动会生成一把钥匙，复制下来
curl http://localhost:8765/health           # {"ok":true}
```

- **钥匙**：不用自己生成。第一次启动时它自己生成一把，打在日志里，也存在 `./data/memory/token`，以后一直用这把。接 Mele 时填在「MCP 服务」的「钥匙」那栏；接别的客户端就是请求头 `Authorization: Bearer <钥匙>`。想自己定：`cp .env.example .env`，填 `MELE_MEMORY_TOKEN`（至少 16 位，比如 `openssl rand -hex 32` 的结果）。

- 起两个容器：`db`（Postgres 17 + pgvector）和 `memory`（MCP 服务 + 网页）。
- MCP 地址 `http://localhost:8765/mcp`，请求头带 `Authorization: Bearer <你的 token>`；网页在 `http://localhost:8765/`。
- 只会旧式 SSE 接法的软件（填的地址要以 `/sse` 结尾那种）：填 `http://localhost:8765/sse`，钥匙一样。链接模式下是 `/sse/<MELE_MEMORY_LINK_SECRET>`。
- 第一次启动会下载 bge-m3 向量模型（约 2.2GB），存在 `./data/models`，之后不再下。数据库在 `./data/pg`。删掉容器、重建镜像，数据都还在。
- 默认只让本机连。要给别的设备连，在 `.env` 里设 `MELE_MEMORY_BIND=0.0.0.0`，并在前面套一层 HTTPS（Caddy、Cloudflare Tunnel 之类）。
- 想每天自动备份：见[设置一览](#设置一览)里的 GitHub 备份。

## 接到 Claude 桌面版

不用 Docker，在自己电脑上跑。需要 Python 3.12、Postgres + [pgvector](https://github.com/pgvector/pgvector)。

```bash
pip install -e ".[embed,mcp]"   # embed = 本地 bge-m3 向量模型（首次下载约 2.2GB）
createdb mele_memory            # 第一次启动会自己建表
```

`claude_desktop_config.json`（Claude 桌面版 → 设置 → 开发者 → 编辑配置）：

```json
{
  "mcpServers": {
    "mele-memory": {
      "command": "/path/to/mele-memory/.venv/bin/python",
      "args": ["-m", "mcp_server"],
      "env": {
        "MEMORY_DSN": "postgresql://localhost/mele_memory",
        "MELE_MEMORY_NAMES": "Lumi,小满",
        "MELE_MEMORY_USER_NAME": "小满",
        "MELE_MEMORY_TZ": "Asia/Shanghai"
      }
    }
  }
}
```

想在本机也看网页：另开一个 `python -m mcp_server --http --port 8765`，连同一个库就行（钥匙同上：不设就自己生成，存在 `~/.mele-memory/token`）。

## 接到 claude.ai 网页版 / 手机

claude.ai 的自定义连接器只能填一个网址，加不了请求头，所以有个**链接模式**。在 Docker 版的 `.env` 里设 `MELE_MEMORY_LINK_SECRET`（至少 32 位字母数字，`openssl rand -hex 32`），连接器网址填：

```
https://<你的域名>/mcp/<MELE_MEMORY_LINK_SECRET>
```

安全上要知道的：

- **这个网址就是钥匙**，拿到它的人能读写这份记忆。别发给别人，别贴到公开的地方。
- 必须走 **HTTPS**，不然网址会明文在网上跑。
- 它跟 `MELE_MEMORY_TOKEN` 是两把钥匙：链接只开 MCP，网页和 `/api/*` 还是要 token。
- 服务自己的访问日志会把它换成 `***`；前面那层代理的日志也记得关掉或脱敏。
- 怀疑泄露了就换一个新的，旧网址立刻失效。
- 比 OAuth 简单，也没 OAuth 安全；适合只给自己用。不设这个值，就完全没有这个入口。

## 网页

浏览器打开 `http://<主机>:8765/`，填上 token：

- **记忆**：按类别筛（记忆、钉住的、人物卡、日记、了结的、远事）；按日期或重要度排，两个方向。每条能钉住、了结、改（标题 / 重要度 / 标签 / 正文）、删。
- **星图**：一个能转的 3D 球。每条记忆是一颗蓝色的 ✨，关联越多颜色越深；人物是金色的星。正中间一颗会呼吸的爱心是用户，停在上面看哪些记忆跟 TA 有关。标签或人挂的记忆够多，那颗星就大一号，点进去飞近只看这些。远事和日记是绕着球的一圈星环。
- **人物**：用户可以自己写、改人物卡。
- **信**：TA 留给下一个自己的、你写给 TA 的。网页上看过不算 TA 读过。
- **状态**：各类几条、向量好没好、核心多少字、备份情况；一键导出全部（JSON）、现在备份。

「关于 TA」（它记的、关于用户的小事）照设计不在页面上显示，导出和备份里有。

页面是 `web/index.html` 一个文件，原生 JS，不引任何外部脚本，随便改样子。接口见[附录](#附录网页接口)。

## 设置一览

| 环境变量 | 作用 |
|---|---|
| `MELE_MEMORY_TOKEN` | MCP 和网页的钥匙，至少 16 位。不设的话 HTTP 模式第一次启动自己生成一把，打在日志里，存在 `MELE_MEMORY_DATA/token` |
| `MELE_MEMORY_DATA` | 放自动生成的钥匙的文件夹，默认 `~/.mele-memory`（Docker 里是 `/data`，对应 `./data/memory`） |
| `MEMORY_DSN` | Postgres 地址，默认 `postgresql://localhost/mele_memory`（Docker 里自动配好） |
| `MELE_MEMORY_NAMES` | 逗号隔开：AI 的名字、昵称、用户的名字（称呼停用词用） |
| `MELE_MEMORY_USER_NAME` | 用户的名字，星图正中间的爱心；正文提到这个名字的记忆连到它 |
| `MELE_MEMORY_TZ` | 用户那边的时区，远事按这边的日子算，默认 `UTC`（例：`Asia/Shanghai`） |
| `MELE_MEMORY_USER` | 这个人的 UUID；不填就用一个固定的默认值 |
| `MELE_MEMORY_LINK_SECRET` | 链接模式，见[接到 claude.ai](#接到-claudeai-网页版--手机) |
| `MELE_MEMORY_BACKUP_REPO` / `MELE_MEMORY_BACKUP_TOKEN` | 每天自动备份到 GitHub **私有**仓库（公开仓库会被拒绝）。token 用细粒度的，只给这一个仓库 Contents 读写。只有一直开着的 HTTP 服务会每天自己跑，状态页也能手动备份 |
| `MELE_MEMORY_BACKUP_HOUR` | 每天几点备份（按 `MELE_MEMORY_TZ`），默认 4 |
| `MELE_MEMORY_BIND` | Docker 端口绑在哪，默认 `127.0.0.1`（只本机） |
| `MELE_MEMORY_FAKE_EMBED` | `1` = 用假向量，只给测试用 |

## 当库用

```python
import memory as M

pool = await M.create_pool(dsn)
await M.apply_schema(dsn)
embedder = M.BgeM3Embedder()

await M.remember(pool, embedder, user_id, "小满对芒果过敏", title="芒果过敏", tags=["健康"], importance=9)
r = await M.recall(pool, embedder, user_id, "今天早餐吃芒果冰沙可以吗")
r.full   # 整条浮上来的记忆
r.lines  # 一行摘要
```

所有查询都按 `user_id` 隔开，别的用户的记忆在最底层就看不见。

潮汐：

```python
from tide import ledger

async def llm(model, system, prompt):          # 接你自己的模型，失败就抛异常
    ...

plan = ledger.plan_roll(messages)              # 哪些卷走、哪些留着
days = ledger.day_transcripts(plan.rolled, "Asia/Shanghai", "zh")
text, tried = await ledger.write_entry(llm, ["deepseek-chat"], days[d][0], d, existing="", name="Lumi", lang="zh")
```

## 开发

```bash
pip install -e ".[embed,mcp,dev]"
pytest                          # 测试用假向量，不用下模型；会临时建一个库，跑完删掉
python eval/run_eval.py --fake  # 评测：一套编出来的记忆和提问（小满、Lumi）；去掉 --fake 用真 bge-m3
```

## 致谢

代码都是我们自己从头写的，但思路受了这些分享的启发：

- **潮汐**（压缩之后注回一本滚动账本）：思路来自 [X @onecrazyfishhh](https://x.com/onecrazyfishhh)
- **记忆的样子**（重要性、情绪正负和强弱、「了结没」、没了结的淡得慢、钉住的核心、混合检索）：[Ombre Brain](https://github.com/P0luz/Ombre-Brain)，作者小红书 鹤见（49651866759）
- **召回的改造**（称呼停用词、召回只读、语义去重、日期窗口要有退路）：缎带骑士 [X @Suisaeki](https://x.com/Suisaeki) 分享的召回链路图
- 向量模型 [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3)（MIT）、精排 bge-reranker-v2-m3（Apache-2.0）

## 许可

源码公开：[PolyForm Noncommercial 1.0.0](LICENSE)

## 附录：网页接口

都要 `Authorization: Bearer <MELE_MEMORY_TOKEN>`。

| 接口 | 作用 |
|---|---|
| `GET /api/memories?kind=&order=new\|old\|important\|unimportant&resolved=1&limit=50&offset=0` | 列记忆；`next_offset` 为空就是没有了 |
| `GET /api/memories/{id}` | 一条记忆 |
| `PATCH /api/memories/{id}` | `{"content", "title", "tags", "importance", "pinned", "resolved"}` 任选几个 |
| `DELETE /api/memories/{id}` | 删掉 |
| `GET /api/graph` | 星图：点（`id`、`label`、`kind`、`weight`、`title`、`tags`）、线（`similar` / `person`）、大一号的星 `hubs`、正中间的 `user`、星环 `ring` |
| `GET /api/people` / `POST /api/people` / `PATCH /api/people/{id}` | 人物卡；网页上写的算用户写的，「是谁 / 要记得」它改不动 |
| `GET /api/dates?resolved=1` / `PATCH /api/dates/{id}` / `DELETE /api/dates/{id}` | 远事；`{"day", "at_time", "title", "note", "resolved"}` |
| `GET /api/letters` / `POST /api/letters` / `DELETE /api/letters/{id}` | 信 |
| `GET /api/status` | 状态 |
| `GET /api/export` | 导出全部（含「关于 TA」和日记）成 JSON |
| `POST /api/backup` | 现在备份一次到 GitHub |

---

## English

Long-term memory for AI companions, extracted from Mele. It remembers what you tell it, lets memories fade with time, and surfaces the relevant ones on its own as you talk.

**Pick a way to use it**

- **Docker** (web page, memory globe, daily backups, reachable from other devices): `docker compose up -d`; on first start it generates a key, prints it in `docker compose logs memory` and saves it to `./data/memory/token` (or set your own `MELE_MEMORY_TOKEN` in `.env`). MCP at `http://localhost:8765/mcp` with `Authorization: Bearer <token>` (clients that only speak the older SSE transport: `http://localhost:8765/sse`, same key); the web page at `http://localhost:8765/`. The bge-m3 model (~2.2GB) downloads on first start into `./data/models`; the database lives in `./data/pg`. Bound to localhost by default; set `MELE_MEMORY_BIND=0.0.0.0` to expose it, with HTTPS in front.
- **Claude Desktop / Cursor on your own machine**: Python 3.12, Postgres + pgvector; `pip install -e ".[embed,mcp]"`, `createdb mele_memory`, then point the client at `python -m mcp_server` (see the `claude_desktop_config.json` example above).
- **claude.ai web and mobile** (URL only, no headers): set `MELE_MEMORY_LINK_SECRET` (32+ letters/digits) and use `https://<host>/mcp/<secret>`. The URL itself is the key: use HTTPS, keep it private, rotate it if it leaks. It opens MCP only, not the web API, and is redacted from the server's access log. Simpler and less secure than OAuth; leave it unset and the door doesn't exist.
- **As a library**: `import memory as M` — see the example above.

**What it does**

- **Remember**: each memory has a title, tags, importance and emotion; near-duplicates merge.
- **Fade**: weight = importance × time decay × emotion boost. Important and unresolved things fade slower; pinned core memories and people cards never fade. Being recalled does not add weight, so one hot topic can't snowball.
- **Recall**: each message is scored against *all* of the user's memories as a z-score, so thresholds hold for small and large libraries and across languages. Three tiers: full memory, one-line summary, or nothing.
- **Hybrid search**: vector + keyword, fused by rank (RRF), times current weight.
- **Name stopwords**: the companion's name, nicknames and the user's name appear in almost every memory and swamp keyword search; per-user frequent terms are stopped automatically.
- **No repeats**: anything already surfaced in this conversation — and its near-twins — is skipped.
- **People cards**, **dates** (things with a day attached; they surface at the top of `recall` from two weeks out), **letters** (to your next self, since every conversation starts from zero), **sticky notes**.
- **Tide (`tide/`)**: when the context grows long, older messages roll into a day-by-day ledger — recent days clear, older days hazy. Each entry is checked sentence by sentence against the source; invented, overlong or third-person entries are rejected. Bring your own model via `llm(model, system, prompt)`.

**MCP tools**: `wake` (once at the start of each conversation: pinned core, weightiest open matters, sticky note, upcoming dates, unread letters), `recall`, `remember` (the chat model writes the title and tags itself; no extra model call), `search`, `list_memories`, `update`, `pin`, `resolve`, `forget`, `person_upsert`, `people`, `date_add`, `dates`, `date_resolve`, `date_forget`, `letter_write`, `letter_read`, `sticky_get`, `sticky_set`.

**Web page**: memories (filter, sort by date or importance either way, edit title / importance / tags / content), a rotatable 3D memory globe (each memory a blue ✨, deeper the more it links to, people in gold; a breathing heart at the center is the user — hover it to see which memories mention them; tags and people with many memories become bigger stars you can fly into; dates and diary entries twinkle in a ring), people cards the user can write, letters, and a status page with one-click export and back-up-now. A single dependency-free `web/index.html`, meant to be restyled.

**Settings**: `MELE_MEMORY_TOKEN` (generated on first HTTP start if unset; `MELE_MEMORY_DATA` says where it's kept), `MEMORY_DSN`, `MELE_MEMORY_NAMES`, `MELE_MEMORY_USER_NAME`, `MELE_MEMORY_TZ`, `MELE_MEMORY_USER`, `MELE_MEMORY_LINK_SECRET`, `MELE_MEMORY_BACKUP_REPO` + `MELE_MEMORY_BACKUP_TOKEN` (daily backup to a **private** GitHub repo; public repos are refused; fine-grained token with Contents read/write on that one repo), `MELE_MEMORY_BACKUP_HOUR`, `MELE_MEMORY_BIND`, `MELE_MEMORY_FAKE_EMBED`.

**Development**: `pip install -e ".[embed,mcp,dev]"`, `pytest` (fake vectors, throwaway database), `python eval/run_eval.py --fake`.

**Credits** (all code written from scratch): the tide idea — a rolling ledger injected back after compaction — comes from [X @onecrazyfishhh](https://x.com/onecrazyfishhh); memory shape inspired by [Ombre Brain](https://github.com/P0luz/Ombre-Brain) (author on Xiaohongshu: 鹤见, 49651866759); recall refinements inspired by the recall pipeline diagram shared by [X @Suisaeki](https://x.com/Suisaeki).

**License**: Source available, [PolyForm Noncommercial 1.0.0](LICENSE)

Required Notice: Copyright Aporia & Poros (https://github.com/aporia-and-poros/mele-memory)
