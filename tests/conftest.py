import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from entities import EntityIndex  # noqa: E402
from models import Article  # noqa: E402
from store import SqliteStore  # noqa: E402

for var in ("GROQ_API_KEY", "SUPABASE_URL", "SUPABASE_KEY", "RESEND_API_KEY", "MAIL_TO", "MAIL_FROM",
            "BSKY_HANDLE", "BSKY_APP_PASSWORD", "DASHBOARD_URL", "LOCAL_DB"):
    os.environ.pop(var, None)

DATA = {
    "houses": [
        {"id": "dior", "label": "Dior", "aliases": ["Dior", "Christian Dior"], "group": "LVMH", "trends": "Dior"},
        {"id": "alaia", "label": "Alaïa", "aliases": ["Alaïa", "Alaia"], "trends": "Alaïa"},
        {"id": "gucci", "label": "Gucci", "aliases": ["Gucci"], "group": "Kering", "trends": "Gucci"},
    ],
    "groups": [{"id": "lvmh", "label": "LVMH", "aliases": ["LVMH", "Moët Hennessy"], "trends": "LVMH"}],
    "people": [
        {"id": "demna", "label": "Demna", "aliases": ["Demna"], "trends": "Demna", "enabled": True},
        {"id": "off", "label": "Off Person", "aliases": ["Off Person"], "enabled": False},
    ],
    "luxury_keywords": {"strong": ["haute couture", "luxe", "luxury"], "weak": ["fashion", "brand", "collection"]},
}


@pytest.fixture
def index():
    return EntityIndex(DATA)


@pytest.fixture
def store():
    return SqliteStore(":memory:")


def day_ago(n: int, hour: int = 10) -> str:
    d = datetime.now(timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=n)
    return d.isoformat()


@pytest.fixture
def make_article():
    counter = {"n": 0}

    def _make(title="Dior unveils a new collection", source="Vogue", days_ago=0, **kw):
        counter["n"] += 1
        kw.setdefault("url", f"https://example.com/{counter['n']}")
        kw.setdefault("country", "🇺🇸")
        kw.setdefault("published", day_ago(days_ago))
        return Article(title=title, source=source, **kw)

    return _make
