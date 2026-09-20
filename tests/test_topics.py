import json
from types import SimpleNamespace

from store import SqliteStore
from topics import (apply_story_matches, cluster_keywords, extract_topic_clusters, match_clusters_to_stories,
                    name_topic_clusters, source_words, term_counts, title_ngrams, tokens)


def test_tokens_drop_noise():
    assert tokens("The Ready-to-Wear show SS27 in Paris 2026") == []
    assert tokens("Demna joins Gucci as creative director") == ["demna", "joins", "gucci", "creative", "director"]
    assert "printemps-eté" not in tokens("Défilé printemps-eté de Chanel")


def test_title_ngrams():
    assert title_ngrams("Gucci hires Demna") == {"gucci hires", "hires demna", "gucci hires demna"}


def make_cluster_articles(make_article):
    return [
        make_article("Demna Gucci debut confirmed for March", source="Vogue"),
        make_article("Demna Gucci: what we know so far", source="WWD"),
        make_article("Inside the Demna Gucci era", source="Business of Fashion"),
        make_article("Unrelated: Prada opens pop-up in Seoul", source="Vogue"),
    ]


def test_extract_topic_clusters_requires_sources_and_size(make_article):
    arts = make_cluster_articles(make_article)
    clusters = extract_topic_clusters(arts, min_articles=3)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["article_count"] == 3 and c["source_count"] == 3 and "demna" in c["phrase"]
    # same outlet repeating itself is not a topic
    same_source = [make_article("Demna Gucci news", source="Vogue") for _ in range(4)]
    assert extract_topic_clusters(same_source, min_articles=3) == []


def test_extract_topic_clusters_ignores_social_posts(make_article):
    posts = [make_article("Demna Gucci", source=f"Bluesky / @u{i}", kind="social") for i in range(4)]
    assert extract_topic_clusters(posts, min_articles=3) == []


def test_generic_phrases_do_not_form_clusters(make_article):
    arts = [make_article("Fashion week highlights", source=s) for s in ("A", "B", "C")]
    assert extract_topic_clusters(arts, min_articles=3) == []


class FakeGroq:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.calls = content, error, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


async def test_name_clusters_uses_groq_labels_and_rejects_generic(make_article):
    clusters = extract_topic_clusters(make_cluster_articles(make_article), min_articles=3)
    client = FakeGroq(json.dumps({"labels": [{"id": 0, "label": "Nomination Demna chez Gucci"}]}))
    await name_topic_clusters(clusters, client, "openai/gpt-oss-120b")
    assert clusters[0]["label"] == "Nomination Demna chez Gucci"
    clusters = extract_topic_clusters(make_cluster_articles(make_article), min_articles=3)
    before = clusters[0]["label"]
    await name_topic_clusters(clusters, FakeGroq(json.dumps({"labels": [{"id": 0, "label": "Fashion"}]})), "m")
    assert clusters[0]["label"] == before


async def test_name_clusters_survives_failure_and_no_client(make_article):
    clusters = extract_topic_clusters(make_cluster_articles(make_article), min_articles=3)
    before = clusters[0]["label"]
    await name_topic_clusters(clusters, FakeGroq(error=RuntimeError("down")), "m")
    await name_topic_clusters(clusters, None, "m")
    assert clusters[0]["label"] == before


def test_story_tracking_across_days(make_article):
    store = SqliteStore(":memory:")
    day1 = extract_topic_clusters(make_cluster_articles(make_article), min_articles=3)
    m1 = match_clusters_to_stories(day1, store.open_stories())
    assert m1 == [None]
    urls1 = apply_story_matches(store, day1, m1, "2026-09-18")
    sid = next(iter(urls1.values()))
    assert store.open_stories()[0]["article_count"] == 3

    day2_arts = [make_article("Demna Gucci debut date revealed", source="Vogue"),
                 make_article("Demna Gucci: what to expect", source="WWD"),
                 make_article("Demna Gucci first look", source="Elle")]
    day2 = extract_topic_clusters(day2_arts, min_articles=3)
    m2 = match_clusters_to_stories(day2, store.open_stories())
    assert m2[0] and m2[0]["id"] == sid
    apply_story_matches(store, day2, m2, "2026-09-19")
    story = store.open_stories()[0]
    assert story["article_count"] == 6 and story["first_seen"] == "2026-09-18" and story["last_seen"] == "2026-09-19"
    assert len(store.open_stories()) == 1

    # unrelated cluster does not attach to the story
    other = extract_topic_clusters([make_article("Prada Miuccia retrospective opens", source=s) for s in ("A", "B", "C")], 3)
    assert match_clusters_to_stories(other, store.open_stories()) == [None]


def test_close_stale_stories():
    store = SqliteStore(":memory:")
    store.save_story({"label": "Old", "first_seen": "2026-09-01", "last_seen": "2026-09-05", "article_count": 3,
                      "status": "open", "keywords": ["a b"]})
    store.save_story({"label": "Fresh", "first_seen": "2026-09-01", "last_seen": "2026-09-19", "article_count": 3,
                      "status": "open", "keywords": ["c d"]})
    assert store.close_stale_stories("2026-09-20") == 1
    assert [s["label"] for s in store.open_stories()] == ["Fresh"]


def test_cluster_keywords_contains_phrase(make_article):
    c = extract_topic_clusters(make_cluster_articles(make_article), min_articles=3)[0]
    assert c["phrase"] in cluster_keywords(c)


def test_term_counts_thresholds_and_exclusions(make_article):
    arts = [make_article("Tariffs hit luxury demand", source="A"), make_article("Tariffs worry Gucci buyers", source="B"),
            make_article("Gucci sneakers", source="A"), make_article("Tariffs everywhere", source="A")]
    counts = term_counts(arts, exclude={"gucci"})
    assert counts["tariffs"] == {"count": 3, "sources": 2}
    assert "gucci" not in counts and "sneakers" not in counts
    one_outlet = [make_article("Tariffs again", source="A") for _ in range(3)]
    assert term_counts(one_outlet, exclude=set()) == {}


def test_source_words(make_article):
    assert source_words([make_article(source="Jing Daily"), make_article(source="Vogue France")]) == \
        {"jing", "daily", "vogue", "france"}
