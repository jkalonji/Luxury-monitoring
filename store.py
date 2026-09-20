"""Persistence: Supabase (production) or a local SQLite file (dev/tests, when SUPABASE_* is unset)."""

import json
import logging
import os
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from models import Article

ARTICLE_COLUMNS = [
    "url", "title", "source", "country", "published", "description", "kind", "lang", "category", "sentiment",
    "entities", "engagement", "hot_topic", "hot_reason", "mention_count", "supa_hot", "summary", "story_id",
    "published_is_estimated",
]
_JSON_COLUMNS = {"entities", "keywords"}
_BOOL_COLUMNS = {"hot_topic", "supa_hot", "published_is_estimated"}
_TABLE_COLUMNS = {
    "articles": ARTICLE_COLUMNS + ["collected_at"],
    "signals": ["entity_id", "entity_type", "metric", "day", "value"],
    "stories": ["id", "label", "first_seen", "last_seen", "article_count", "status", "keywords"],
}


def article_row(a: Article) -> dict:
    row = {k: v for k, v in asdict(a).items() if k in ARTICLE_COLUMNS}
    return row


def _since_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _since_day(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


_ORDER = {"articles": ["url"], "stories": ["id"], "signals": ["entity_id", "metric", "day"]}   # unique keys: stable paging


def _dedup(rows: list[dict], conflict: str) -> list[dict]:
    """Last row wins per conflict key (Postgres refuses an upsert that touches the same row twice)."""
    keys = conflict.split(",")
    return list({tuple(r[k] for k in keys): r for r in rows}.values())


class Store(ABC):
    """Domain API used by the pipeline, dashboard and mailer. Subclasses provide 4 primitives."""

    @abstractmethod
    def _upsert(self, table: str, rows: list[dict], conflict: str) -> None: ...

    @abstractmethod
    def _select(self, table: str, gte: tuple[str, str] | None = None, eq: dict | None = None) -> list[dict]: ...

    @abstractmethod
    def _insert(self, table: str, row: dict) -> int: ...

    @abstractmethod
    def _update(self, table: str, row_id: int, values: dict) -> None: ...

    # -- articles ----------------------------------------------------------
    def recent_urls(self, days: int = 14) -> set[str]:
        return {r["url"] for r in self._select("articles", gte=("published", _since_iso(days)))}

    def save_articles(self, articles: list[Article]) -> int:
        rows = _dedup([article_row(a) for a in articles], "url")
        if rows:
            self._upsert("articles", rows, "url")
        return len(rows)

    def load_articles(self, days: int) -> list[dict]:
        rows = self._select("articles", gte=("published", _since_iso(days)))
        return sorted(rows, key=lambda r: r["published"], reverse=True)

    # -- signals -----------------------------------------------------------
    def save_signals(self, rows: list[dict]) -> int:
        rows = _dedup(rows, "entity_id,metric,day")
        if rows:
            self._upsert("signals", rows, "entity_id,metric,day")
        return len(rows)

    def load_signals(self, days: int) -> list[dict]:
        return self._select("signals", gte=("day", _since_day(days)))

    # -- stories -----------------------------------------------------------
    def open_stories(self) -> list[dict]:
        return self._select("stories", eq={"status": "open"})

    def all_stories(self, days: int) -> list[dict]:
        return self._select("stories", gte=("last_seen", _since_day(days)))

    def save_story(self, story: dict) -> int:
        story = dict(story)
        story_id = story.pop("id", None)
        if story_id is None:
            return self._insert("stories", story)
        self._update("stories", story_id, story)
        return story_id

    def close_stale_stories(self, today: str, idle_days: int = 4) -> int:
        cutoff = (datetime.fromisoformat(today) - timedelta(days=idle_days)).strftime("%Y-%m-%d")
        stale = [s for s in self.open_stories() if s["last_seen"] < cutoff]
        for s in stale:
            self._update("stories", s["id"], {"status": "closed"})
        return len(stale)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY, title TEXT NOT NULL, source TEXT, country TEXT, published TEXT NOT NULL,
    description TEXT, kind TEXT DEFAULT 'article', lang TEXT, category TEXT, sentiment TEXT, entities TEXT DEFAULT '[]',
    engagement INTEGER DEFAULT 0, hot_topic INTEGER DEFAULT 0, hot_reason TEXT DEFAULT '', mention_count INTEGER DEFAULT 0,
    supa_hot INTEGER DEFAULT 0, summary TEXT DEFAULT '', story_id INTEGER, published_is_estimated INTEGER DEFAULT 0,
    collected_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS signals (
    entity_id TEXT NOT NULL, entity_type TEXT NOT NULL, metric TEXT NOT NULL, day TEXT NOT NULL, value REAL NOT NULL,
    PRIMARY KEY (entity_id, metric, day));
CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    article_count INTEGER DEFAULT 0, status TEXT DEFAULT 'open', keywords TEXT DEFAULT '[]');
"""


class SqliteStore(Store):
    def __init__(self, path: str = "data/local.db"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SQLITE_SCHEMA)

    @staticmethod
    def _to_db(row: dict) -> dict:
        return {k: (json.dumps(v, ensure_ascii=False) if k in _JSON_COLUMNS else int(v) if isinstance(v, bool) else v)
                for k, v in row.items()}

    @staticmethod
    def _from_db(row: sqlite3.Row) -> dict:
        d = dict(row)
        for k in _JSON_COLUMNS & d.keys():
            d[k] = json.loads(d[k] or "[]")
        for k in _BOOL_COLUMNS & d.keys():
            d[k] = bool(d[k])
        return d

    def _upsert(self, table, rows, conflict):
        keys = conflict.split(",")
        for row in map(self._to_db, rows):
            cols = list(row)
            updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
            sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                   f"ON CONFLICT({conflict}) DO UPDATE SET {updates}")
            self.conn.execute(sql, [row[c] for c in cols])
        self.conn.commit()

    def _select(self, table, gte=None, eq=None):
        sql, params = f"SELECT * FROM {table}", []
        clauses = []
        if gte:
            clauses.append(f"{gte[0]} >= ?")
            params.append(gte[1])
        for k, v in (eq or {}).items():
            clauses.append(f"{k} = ?")
            params.append(v)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [self._from_db(r) for r in self.conn.execute(sql, params)]

    def _insert(self, table, row):
        row = self._to_db(row)
        cur = self.conn.execute(
            f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        self.conn.commit()
        return cur.lastrowid

    def _update(self, table, row_id, values):
        values = self._to_db(values)
        self.conn.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in values)} WHERE id=?",
                          [*values.values(), row_id])
        self.conn.commit()


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

class SupabaseStore(Store):
    PAGE = 1000
    CHUNK = 500

    def __init__(self, url: str, key: str, client=None):
        if client is None:
            from supabase import create_client
            client = create_client(url, key)
        self.client = client

    def _upsert(self, table, rows, conflict):
        for i in range(0, len(rows), self.CHUNK):
            self.client.table(table).upsert(rows[i:i + self.CHUNK], on_conflict=conflict).execute()

    def _select(self, table, gte=None, eq=None):
        out, start = [], 0
        while True:
            q = self.client.table(table).select("*")
            if gte:
                q = q.gte(*gte)
            for k, v in (eq or {}).items():
                q = q.eq(k, v)
            for col in _ORDER[table]:
                q = q.order(col)
            page = q.range(start, start + self.PAGE - 1).execute().data or []
            out.extend(page)
            if len(page) < self.PAGE:
                return out
            start += self.PAGE

    def _insert(self, table, row):
        return self.client.table(table).insert(row).execute().data[0]["id"]

    def _update(self, table, row_id, values):
        self.client.table(table).update(values).eq("id", row_id).execute()


def get_store() -> Store:
    """Supabase when SUPABASE_URL/SUPABASE_KEY are set, otherwise a local SQLite file."""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if url and key:
        return SupabaseStore(url, key)
    path = os.environ.get("LOCAL_DB", "data/local.db")
    logging.warning(f"SUPABASE_URL/SUPABASE_KEY not set - using local SQLite ({path})")
    return SqliteStore(path)
