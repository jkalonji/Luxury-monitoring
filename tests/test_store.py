import re
from datetime import datetime, timedelta, timezone

import pytest

from models import Article
from store import ARTICLE_COLUMNS, SqliteStore, SupabaseStore, _TABLE_COLUMNS, article_row, get_store


def test_article_roundtrip(store, make_article):
    a = make_article("Dior wins", entities=["dior", "lvmh"], hot_topic=True, hot_reason="Topic", mention_count=4,
                     supa_hot=True, summary="Résumé", category="Business & Finance", sentiment="Positif",
                     published_is_estimated=True, description="é à ü ✓")
    assert store.save_articles([a]) == 1
    (row,) = store.load_articles(2)
    assert row["entities"] == ["dior", "lvmh"] and row["hot_topic"] is True and row["supa_hot"] is True
    assert row["published_is_estimated"] is True and row["description"] == "é à ü ✓" and row["summary"] == "Résumé"
    assert row["collected_at"]
    assert set(ARTICLE_COLUMNS) <= set(row)


def test_article_upsert_is_idempotent_and_updates(store, make_article):
    a = make_article("Dior wins", url="https://x/1", sentiment="Neutre")
    store.save_articles([a])
    a.sentiment = "Positif"
    store.save_articles([a, a])         # duplicate in the same batch must not break
    rows = store.load_articles(2)
    assert len(rows) == 1 and rows[0]["sentiment"] == "Positif"


def test_load_articles_window_and_order(store, make_article):
    store.save_articles([make_article("old", days_ago=20), make_article("mid", days_ago=3), make_article("new", days_ago=0)])
    assert [r["title"] for r in store.load_articles(10)] == ["new", "mid"]
    assert store.recent_urls(10) == {r["url"] for r in store.load_articles(10)}


def test_not_persisted_fields_are_dropped(make_article):
    row = article_row(make_article("x", filter="luxury", relevant=False))
    assert "filter" not in row and "relevant" not in row


def recent_day(n=0):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def test_signals_upsert(store):
    row = {"entity_id": "dior", "entity_type": "house", "metric": "articles", "day": recent_day(), "value": 3.0}
    store.save_signals([row, {**row, "value": 5.0}])
    store.save_signals([{**row, "value": 7.0}])
    rows = store.load_signals(10)
    assert [r["value"] for r in rows] == [7.0]


def test_stories_crud(store):
    sid = store.save_story({"label": "A", "first_seen": "2026-09-18", "last_seen": "2026-09-18", "article_count": 3,
                            "status": "open", "keywords": ["a b", "c d"]})
    assert store.open_stories()[0]["keywords"] == ["a b", "c d"]
    store.save_story({"id": sid, "label": "A", "first_seen": "2026-09-18", "last_seen": "2026-09-19",
                      "article_count": 6, "status": "open", "keywords": ["a b"]})
    (s,) = store.open_stories()
    assert s["article_count"] == 6 and s["last_seen"] == "2026-09-19"


def test_get_store_defaults_to_sqlite_and_honours_local_db(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_DB", str(tmp_path / "x.db"))
    s = get_store()
    assert isinstance(s, SqliteStore) and (tmp_path / "x.db").exists()


def test_get_store_uses_supabase_when_configured(monkeypatch):
    import store as store_mod
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "k")
    monkeypatch.setattr(store_mod, "SupabaseStore", lambda url, key: ("supabase", url, key))
    assert get_store() == ("supabase", "https://x.supabase.co", "k")


# ---- Supabase (fake PostgREST client) -----------------------------------------

class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table_name = db, table
        self.filters, self.orders, self.rng, self.op, self.payload, self.conflict = [], [], None, "select", None, None

    def select(self, _):
        return self

    def gte(self, col, val):
        self.filters.append((col, ">=", val))
        return self

    def eq(self, col, val):
        self.filters.append((col, "=", val))
        return self

    def order(self, col):
        self.orders.append(col)
        return self

    def range(self, a, b):
        self.rng = (a, b)
        return self

    def upsert(self, rows, on_conflict):
        self.op, self.payload, self.conflict = "upsert", rows, on_conflict
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def update(self, values):
        self.op, self.payload = "update", values
        return self

    def execute(self):
        db, t = self.db, self.table_name
        if self.op == "upsert":
            db.upserts.append((t, len(self.payload), self.conflict))
            keys = self.conflict.split(",")
            keys_seen = [tuple(r[k] for k in keys) for r in self.payload]
            assert len(keys_seen) == len(set(keys_seen)), "duplicate keys in one upsert (Postgres would reject it)"
            for r in self.payload:
                key = tuple(r[k] for k in keys)
                db.rows[t] = [x for x in db.rows[t] if tuple(x[k] for k in keys) != key] + [dict(r)]
            return type("R", (), {"data": self.payload})
        if self.op == "insert":
            row = {**self.payload, "id": len(db.rows[t]) + 1}
            db.rows[t].append(row)
            return type("R", (), {"data": [row]})
        rows = db.rows[t]
        for col, op, val in self.filters:
            rows = [r for r in rows if (r[col] >= val if op == ">=" else r[col] == val)]
        if self.op == "update":
            for r in rows:
                r.update(self.payload)
            return type("R", (), {"data": rows})
        db.selects.append((t, tuple(self.orders), self.rng))
        rows = sorted(rows, key=lambda r: tuple(r[c] for c in self.orders))
        a, b = self.rng
        return type("R", (), {"data": rows[a:b + 1]})


class FakeSupabase:
    def __init__(self):
        self.rows = {"articles": [], "signals": [], "stories": []}
        self.upserts, self.selects = [], []

    def table(self, name):
        return FakeQuery(self, name)


def test_supabase_store_paginates_with_unique_order_and_chunks_upserts(make_article):
    client = FakeSupabase()
    st = SupabaseStore("u", "k", client=client)
    st.PAGE, st.CHUNK = 7, 5
    signals = [{"entity_id": f"e{i % 4}", "entity_type": "house", "metric": "articles", "day": recent_day(i // 4),
                "value": float(i)} for i in range(20)]
    assert st.save_signals(signals + signals[:3]) == 20       # duplicates collapsed before sending
    assert [n for _, n, _ in client.upserts] == [5, 5, 5, 5]
    loaded = st.load_signals(30)
    assert len(loaded) == 20 and len({(r["entity_id"], r["day"]) for r in loaded}) == 20     # nothing lost/duplicated by paging
    assert all(order == ("entity_id", "metric", "day") for t, order, _ in client.selects if t == "signals")
    assert len([s for s in client.selects if s[0] == "signals"]) == 3     # 7 + 7 + 6


def test_supabase_store_articles_and_stories(make_article):
    client = FakeSupabase()
    st = SupabaseStore("u", "k", client=client)
    a = make_article("Dior wins", url="https://x/1")
    assert st.save_articles([a, a]) == 1
    assert len(client.rows["articles"]) == 1
    sid = st.save_story({"label": "S", "first_seen": "2026-09-18", "last_seen": "2026-09-18", "article_count": 3,
                         "status": "open", "keywords": ["a b"]})
    assert sid == 1 and st.open_stories()[0]["label"] == "S"
    st.save_story({"id": sid, "label": "S", "first_seen": "2026-09-18", "last_seen": "2026-09-19", "article_count": 6,
                   "status": "open", "keywords": ["a b"]})
    assert st.open_stories()[0]["article_count"] == 6
    assert st.close_stale_stories("2026-09-30") == 1 and st.open_stories() == []


# ---- schema.sql must match the columns the code writes ------------------------------

def test_schema_sql_matches_store_columns():
    sql = open("schema.sql", encoding="utf-8").read()
    for table, cols in _TABLE_COLUMNS.items():
        m = re.search(rf"create table if not exists {table}\s*\((.*?)\n\);", sql, re.S | re.I)
        assert m, f"table {table} missing in schema.sql"
        body = m.group(1).lower()
        for col in cols:
            assert re.search(rf"(^|\n)\s*{col}\s", body), f"{table}.{col} missing in schema.sql"
    assert "row level security" in sql.lower()
