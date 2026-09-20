import json
from types import SimpleNamespace

import pytest

import classify
from classify import _system_prompt, classify_articles, fallback_classify, groq_model, llm_kwargs
from models import CATEGORIES, SENTIMENTS


class FakeGroq:
    """Minimal AsyncGroq stand-in: `handler(messages) -> str | Exception` is called per request."""

    def __init__(self, handler):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.handler = handler

    async def create(self, **kw):
        self.calls.append(kw)
        out = self.handler(kw)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=out))])


def items_for(kw, overrides=None):
    overrides = overrides or {}
    payload = json.loads(kw["messages"][1]["content"])
    return [{"id": p["id"], "category": "Business & Finance", "sentiment": "Positif", "relevant": True,
             "summary": "Résumé.", **overrides.get(p["id"], {})} for p in payload]


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr(classify.asyncio, "sleep", fast)


def test_prompt_lists_all_ten_categories():
    prompt = _system_prompt()
    assert len(CATEGORIES) == 10
    for c in CATEGORIES:
        assert c in prompt
    assert "RSE" in CATEGORIES and "Durabilité" not in CATEGORIES


def test_fallback_rules(make_article):
    cases = [
        ("Chanel ouvre une boutique éphémère à Tokyo", "Ouverture de boutique / pop-up"),
        ("Gucci names a new creative director", "Nominations & Créatifs"),
        ("Balenciaga face à une polémique", "Drama & Controverses"),
        ("LVMH: revenue drops in Q3", "Business & Finance"),
        ("Dior show opens Paris fashion week", "Collections & Défilés"),
        ("Le luxe durable séduit", "RSE"),
        ("Something with no keyword", "Actualités des Maisons de luxe"),
    ]
    for title, expected in cases:
        cat, sent = fallback_classify(make_article(title))
        assert cat == expected, (title, cat)
        assert sent in SENTIMENTS
    assert fallback_classify(make_article("Record growth for Hermès"))[1] == "Positif"
    assert fallback_classify(make_article("Sales fall and profit warning"))[1] == "Négatif"


async def test_without_client_uses_fallback(make_article):
    arts = [make_article("Dior show opens Paris fashion week")]
    out = await classify_articles(arts, client=None)
    assert out[0].category == "Collections & Défilés" and out[0].sentiment in SENTIMENTS


async def test_groq_happy_path_batches_of_15(make_article):
    arts = [make_article(f"Story {i}", url=f"https://x/{i}") for i in range(31)]
    client = FakeGroq(lambda kw: json.dumps({"items": items_for(kw)}))
    out = await classify_articles(arts, client=client, model="openai/gpt-oss-120b")
    assert len(client.calls) == 3 and len(out) == 31
    assert {a.category for a in out} == {"Business & Finance"} and {a.sentiment for a in out} == {"Positif"}
    assert client.calls[0]["response_format"] == {"type": "json_object"}
    assert client.calls[0]["reasoning_effort"] == "low"


def test_reasoning_effort_only_for_reasoning_models():
    assert llm_kwargs("openai/gpt-oss-120b") == {"reasoning_effort": "low"}
    assert llm_kwargs("llama-3.3-70b-versatile") == {}


async def test_groq_summary_kept_only_for_hot_articles(make_article):
    hot, cold = make_article("Hot", hot_topic=True), make_article("Cold")
    client = FakeGroq(lambda kw: json.dumps({"items": items_for(kw)}))
    await classify_articles([hot, cold], client=client)
    assert hot.summary == "Résumé." and cold.summary == ""


async def test_groq_invalid_values_fall_back_per_field(make_article):
    a = make_article("Dior show opens Paris fashion week")
    client = FakeGroq(lambda kw: json.dumps({"items": items_for(kw, {0: {"category": "Nonsense", "sentiment": "Positif"}})}))
    await classify_articles([a], client=client)
    assert a.category == "Collections & Défilés" and a.sentiment == "Positif"


async def test_groq_missing_items_use_fallback(make_article):
    arts = [make_article("Dior show opens Paris fashion week"), make_article("Chanel ouvre une boutique")]
    client = FakeGroq(lambda kw: json.dumps({"items": items_for(kw)[:1]}))
    await classify_articles(arts, client=client)
    assert arts[0].category == "Business & Finance"          # from Groq
    assert arts[1].category == "Ouverture de boutique / pop-up"   # from fallback


@pytest.mark.parametrize("bad", ["not json at all", json.dumps({"items": []}), json.dumps({"other": 1}), RuntimeError("429")])
async def test_groq_failure_falls_back_and_reports(make_article, bad, capsys):
    arts = [make_article("Dior show opens Paris fashion week")]
    client = FakeGroq(lambda kw: bad)
    out = await classify_articles(arts, client=client)
    assert out[0].category == "Collections & Défilés" and out[0].sentiment in SENTIMENTS
    assert "::error::" in capsys.readouterr().out       # 1/1 batch failed (> 20%)


async def test_relevance_veto_only_on_filtered_sources(make_article):
    generalist = make_article("Election results", filter="luxury")
    magazine = make_article("Election results", url="https://x/other")
    kept = make_article("Gucci spring", url="https://x/kept", filter="luxury")
    client = FakeGroq(lambda kw: json.dumps({"items": items_for(kw, {0: {"relevant": False}})}))
    out = await classify_articles([generalist, magazine, kept], client=client)
    assert [a.url for a in out] == [magazine.url, kept.url]


def test_groq_model_env(monkeypatch):
    assert groq_model() == "openai/gpt-oss-120b"
    monkeypatch.setenv("GROQ_MODEL", "'some/model' ")
    assert groq_model() == "some/model"
