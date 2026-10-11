-- 记忆服务的表。重复执行安全（IF NOT EXISTS）。
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS memories (
  id               BIGSERIAL PRIMARY KEY,
  user_id          UUID        NOT NULL,
  kind             TEXT        NOT NULL DEFAULT 'memory'
                   CHECK (kind IN ('memory', 'core', 'person', 'about', 'diary')),
  content          TEXT        NOT NULL,
  title            TEXT        NOT NULL DEFAULT '',   -- 一行标题（10-04），可空
  name             TEXT,                       -- 人物卡才有：名字
  aliases          TEXT[]      NOT NULL DEFAULT '{}',  -- 人物卡：别名
  relation         TEXT,                       -- 人物卡：是谁（跟用户的关系）
  impression       TEXT        NOT NULL DEFAULT '',   -- 人物卡：它自己的印象（content 是用户写的「要记得」）
  created_by       TEXT        NOT NULL DEFAULT 'user' CHECK (created_by IN ('user', 'ai')),
  updated_by       TEXT        NOT NULL DEFAULT 'user' CHECK (updated_by IN ('user', 'ai')),
  importance       SMALLINT    NOT NULL DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
  valence          REAL        NOT NULL DEFAULT 0 CHECK (valence BETWEEN -1 AND 1),
  arousal          REAL        NOT NULL DEFAULT 0 CHECK (arousal BETWEEN 0 AND 1),
  tags             TEXT[]      NOT NULL DEFAULT '{}',
  resolved         BOOLEAN     NOT NULL DEFAULT FALSE,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_recalled_at TIMESTAMPTZ,
  recall_count     INT         NOT NULL DEFAULT 0,
  rewritten_at     TIMESTAMPTZ,                -- 内容最后一次改写/合并（淡去的时钟从这里重新算）
  embedding        vector(1024),               -- 算不出来时先空着，backfill 补
  search_tsv       tsvector                    -- jieba 分好的词
);

-- 每个用户几千条以内，按 user_id 过滤后精确算相似度就够快；
-- 不建 HNSW：带 user_id 过滤时近似索引会漏结果。
-- 2026-09-26 加「关于 TA」（about）：老库的约束换成新的
ALTER TABLE memories ADD COLUMN IF NOT EXISTS rewritten_at TIMESTAMPTZ;
ALTER TABLE memories ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '';   -- 10-04
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_kind_check;
ALTER TABLE memories ADD CONSTRAINT memories_kind_check CHECK (kind IN ('memory', 'core', 'person', 'about', 'diary'));   -- diary：Ta 的日记（10-01）

CREATE INDEX IF NOT EXISTS memories_user_kind_idx ON memories (user_id, kind);
CREATE INDEX IF NOT EXISTS memories_tsv_idx ON memories USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS memories_no_embedding_idx ON memories (id) WHERE embedding IS NULL;

CREATE TABLE IF NOT EXISTS sticky_notes (
  user_id    UUID        PRIMARY KEY,
  text       TEXT        NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 远事（10-04）：有日子、要惦记着的事。日子是用户那边的日期；at_time 'HH:MM' 或空
CREATE TABLE IF NOT EXISTS far_dates (
  id          BIGSERIAL   PRIMARY KEY,
  user_id     UUID        NOT NULL,
  day         DATE        NOT NULL,
  at_time     TEXT        NOT NULL DEFAULT '',
  title       TEXT        NOT NULL,
  note        TEXT        NOT NULL DEFAULT '',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ,
  result      TEXT        NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS far_dates_user_day_idx ON far_dates (user_id, day);

-- 信（10-04）：给下一个窗口的自己留的信 / 用户写给它的信。原文永久保留
CREATE TABLE IF NOT EXISTS letters (
  id         BIGSERIAL   PRIMARY KEY,
  user_id    UUID        NOT NULL,
  author     TEXT        NOT NULL CHECK (author IN ('ai', 'user')),
  title      TEXT        NOT NULL DEFAULT '',
  content    TEXT        NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  read_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS letters_user_created_idx ON letters (user_id, created_at DESC);
