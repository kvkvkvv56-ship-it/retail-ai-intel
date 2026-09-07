"""数据层：JSONL 真相源 ↔ SQLite 运行时索引

设计见 docs/技术方案.md §2.2、§14。

  data/*.jsonl   真相源，git 提交，行级 diff，GitHub 网页可直接阅读
  intel.db       运行时索引 + FTS5，gitignore，可随时从 JSONL 重建

  rebuild()  JSONL → SQLite   （clone 后 5 秒可查）
  dump()     SQLite → JSONL   （每轮运行结束）
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = ROOT / "intel.db"

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS sources (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  org             TEXT NOT NULL,
  source_class    TEXT NOT NULL CHECK(source_class IN ('A','B','C','D','E')),
  domains         TEXT,
  channel         TEXT NOT NULL,
  channel_ref     TEXT,
  affiliated_with TEXT,
  reliability     REAL DEFAULT 1.0,
  partial_content INTEGER DEFAULT 0,
  active          INTEGER DEFAULT 1,
  note            TEXT
);

CREATE TABLE IF NOT EXISTS items (
  id              TEXT PRIMARY KEY,
  url             TEXT UNIQUE NOT NULL,
  url_canonical   TEXT NOT NULL,
  title           TEXT NOT NULL,
  content         TEXT,
  source_id       TEXT,
  source_class    TEXT,
  original_source TEXT,
  effective_class TEXT,
  published_at    TEXT,
  time_source     TEXT CHECK(time_source IN ('exact','parsed','inferred',NULL)),
  discovered_at   TEXT NOT NULL,
  run_id          TEXT,
  channel         TEXT,
  simhash         TEXT,
  status          TEXT DEFAULT 'pending',
  event_id        TEXT,
  draft_stage     TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_run    ON items(run_id);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_canon  ON items(url_canonical);

CREATE TABLE IF NOT EXISTS events (
  id               TEXT PRIMARY KEY,
  event_key        TEXT NOT NULL,
  title            TEXT NOT NULL,
  summary          TEXT,
  company          TEXT NOT NULL,
  domains          TEXT,
  stage            TEXT,
  stage_basis      TEXT,
  confidence       TEXT,
  status           TEXT,
  first_seen_at    TEXT,
  last_update_at   TEXT,
  event_date       TEXT,
  independent_orgs INTEGER DEFAULT 0,
  review_state     TEXT DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS claims (
  id                  TEXT PRIMARY KEY,
  event_id            TEXT NOT NULL,
  kind                TEXT NOT NULL CHECK(kind IN ('fact','inference','recommendation')),
  text                TEXT NOT NULL,
  source_item_id      TEXT,
  based_on            TEXT,
  attributed_to       TEXT,
  audience            TEXT,
  needs_internal_data INTEGER
);

CREATE TABLE IF NOT EXISTS edges (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  from_event TEXT NOT NULL,
  to_event   TEXT NOT NULL,
  relation   TEXT NOT NULL,
  basis      TEXT NOT NULL,
  created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejects (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT,
  item_id       TEXT,
  url           TEXT,
  title         TEXT,
  source_name   TEXT,
  stage         TEXT NOT NULL,
  reason_code   TEXT NOT NULL,
  reason_detail TEXT,
  merged_into   TEXT,
  created_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_rejects_run ON rejects(run_id);

CREATE TABLE IF NOT EXISTS runs (
  id          TEXT PRIMARY KEY,
  started_at  TEXT,
  finished_at TEXT,
  mode        TEXT,
  dataset     TEXT,
  stats       TEXT,
  cost        TEXT,
  config_hash TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT, target_id TEXT, action TEXT,
  note        TEXT, reviewer TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS reports (
  id           TEXT PRIMARY KEY,
  period_start TEXT, period_end TEXT,
  headline     TEXT, body TEXT,
  generated_at TEXT, run_id TEXT
);
"""

# 表 → 列（决定 JSONL 的字段顺序，也用于 dump/rebuild）
TABLES: dict[str, list[str]] = {
    "sources": ["id", "name", "org", "source_class", "domains", "channel", "channel_ref",
                "affiliated_with", "reliability", "partial_content", "active", "note"],
    "items": ["id", "url", "url_canonical", "title", "content", "source_id", "source_class",
              "original_source", "effective_class", "published_at", "time_source",
              "discovered_at", "run_id", "channel", "simhash", "status", "event_id",
              "draft_stage"],
    "events": ["id", "event_key", "title", "summary", "company", "domains", "stage",
               "stage_basis", "confidence", "status", "first_seen_at", "last_update_at",
               "event_date", "independent_orgs", "review_state"],
    "claims": ["id", "event_id", "kind", "text", "source_item_id", "based_on",
               "attributed_to", "audience", "needs_internal_data"],
    "edges": ["from_event", "to_event", "relation", "basis", "created_by"],
    "rejects": ["run_id", "item_id", "url", "title", "source_name", "stage",
                "reason_code", "reason_detail", "merged_into", "created_at"],
    "runs": ["id", "started_at", "finished_at", "mode", "dataset", "stats", "cost", "config_hash"],
    "reviews": ["target_type", "target_id", "action", "note", "reviewer", "created_at"],
    "reports": ["id", "period_start", "period_end", "headline", "body", "generated_at", "run_id"],
}

# 这些表按追加语义 dump（无稳定主键，靠内容去重）
APPEND_TABLES = {"edges", "rejects", "reviews"}


class Store:
    def __init__(self, db_path: Path = DB_PATH):
        self.path = db_path
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """CREATE TABLE IF NOT EXISTS 不会给已存在的表补列。
        新增列必须在这里显式补，否则老库跑新代码会静默走空值分支。"""
        for table, col, ddl in (("items", "draft_stage", "TEXT"),):
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    # ---------------------------------------------------------------- 基础
    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def upsert(self, table: str, row: dict) -> None:
        cols = [c for c in TABLES[table] if c in row]
        ph = ",".join("?" * len(cols))
        self.db.execute(
            f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({ph})",
            [_enc(row[c]) for c in cols],
        )

    def insert(self, table: str, row: dict) -> None:
        cols = [c for c in TABLES[table] if c in row]
        ph = ",".join("?" * len(cols))
        self.db.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph})",
            [_enc(row[c]) for c in cols],
        )

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        r = self.db.execute(sql, args).fetchone()
        return r[0] if r else None

    def commit(self) -> None:
        self.db.commit()

    # ---------------------------------------------------- JSONL 真相源同步
    def dump(self) -> dict[str, int]:
        """SQLite → data/*.jsonl。每轮运行结束调用。"""
        DATA.mkdir(parents=True, exist_ok=True)
        counts = {}
        for table, cols in TABLES.items():
            rows = self.db.execute(f"SELECT * FROM {table}").fetchall()
            order = "id" if "id" in [d[1] for d in self.db.execute(
                f"PRAGMA table_info({table})")] and table not in APPEND_TABLES else None
            recs = [{c: _dec(r[c]) for c in cols if c in r.keys()} for r in rows]
            if order:
                recs.sort(key=lambda x: str(x.get("id", "")))
            lines = [json.dumps(r, ensure_ascii=False, sort_keys=False) for r in recs]
            (DATA / f"{table}.jsonl").write_text(
                "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            counts[table] = len(recs)
        return counts

    def rebuild(self) -> dict[str, int]:
        """data/*.jsonl → SQLite。clone 后或人工复核前调用。"""
        counts = {}
        for table in TABLES:
            f = DATA / f"{table}.jsonl"
            self.db.execute(f"DELETE FROM {table}")
            n = 0
            if f.exists():
                for line in f.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        self.upsert(table, json.loads(line))
                        n += 1
            counts[table] = n
        self.db.commit()
        return counts


def _enc(v):
    """list/dict 落库前转 JSON 字符串；bool 转 0/1。"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


def _dec(v):
    """从库里读出时还原 JSON 字段。"""
    if isinstance(v, str) and v[:1] in "[{" and v[-1:] in "]}":
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v
