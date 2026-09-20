"""Topic detection: n-gram clustering of titles, Groq naming, cross-day story tracking, term counts."""

import json
import logging
import re
from collections import Counter, defaultdict

from classify import llm_kwargs
from models import Article

# ---------------------------------------------------------------------------
# Stopwords (FR + EN) — generic words that carry no topic identity
# ---------------------------------------------------------------------------

_EN = """the and but for with from that this these those its not new how why what when where who which more can all out
over about into than their they there says said just also after amid will would could should have has had been are was were
you your our his her him she them then here even like time year years week weeks ways most some many much first last one two
three back now next each such does did being get gets got make makes made take takes look looks see sees show shows only
still already inside before between while through against without around within via per too very""".split()
_FR = """les des une dans pour avec sur par que qui sont était été être avoir fait faire plus moins très aussi mais donc
car est ont ses son sa leur leurs cette ces cet nous vous ils elles lui elle entre après avant sans sous chez comme
tout tous toute toutes encore déjà depuis pendant vers contre selon dont où quand comment pourquoi quoi
peut peuvent doit doivent nouveau nouvelle nouveaux nouvelles premier première dernier dernière ans jour jours
semaine semaines mois année années fois""".split()
_GENERIC = """fashion mode luxury luxe collection collections brand brands maison maisons house houses style news
launch launches launched unveils unveiled reveals revealed announces announced presents présente dévoile lance
season saison campaign campagne look looks stories story guide best top list listes édition edition
magazine online store stores article video photos photo inside behind
page search latest assistant manager spring summer autumn winter fall women womens men mens ready wear prêt porter
printemps été eté automne hiver défilé défilés week paris london milan york italy france rome tokyo
today aujourd hui report reports read watch shop shopping buy sale sales deals exclusive interview
weekend cette celle celui ceux dont mieux plus bien tout tous according associate part daily""".split()

STOPWORDS: set[str] = set(_EN) | set(_FR) | set(_GENERIC)

# Phrases too generic to define a topic on their own.
GENERIC_NGRAMS = {
    "fashion week", "fashion weeks", "semaine mode", "haute couture", "prêt-à-porter", "ready-to-wear",
    "new collection", "nouvelle collection", "spring summer", "fall winter", "printemps été", "automne hiver",
    "red carpet", "tapis rouge", "luxury brand", "luxury brands", "luxury fashion", "grand public",
}

_TOKEN_RE = re.compile(r"[a-zà-ÿ0-9][a-zà-ÿ0-9\-]*")


_SEASON_CODE_RE = re.compile(r"^(?:ss|fw|aw|ah|pe|cr)\d{2}$")   # ss27, fw26...


def tokens(text: str, min_len: int = 3) -> list[str]:
    """Lowercased content tokens. Dropped: stopwords, numbers, season codes, short tokens and
    hyphenated compounds containing a stopword ('ready-to-wear', 'printemps-eté'). Mirrored in dashboard_template.html."""
    out = []
    for w in _TOKEN_RE.findall(text.lower()):
        if len(w) < min_len or w in STOPWORDS or w.isdigit() or _SEASON_CODE_RE.match(w):
            continue
        if "-" in w and any(part in STOPWORDS for part in w.split("-")):
            continue
        out.append(w)
    return out


def source_words(articles) -> set[str]:
    """Tokens of the source names themselves ('jing', 'daily', 'vogue'...) - they say nothing about a topic."""
    return {t for a in articles for t in re.findall(r"[a-zà-ÿ0-9]+", a.source.lower())}


def title_ngrams(title: str) -> set[str]:
    words = tokens(title)
    grams: set[str] = set()
    for i in range(len(words) - 1):
        grams.add(f"{words[i]} {words[i + 1]}")
    for i in range(len(words) - 2):
        grams.add(f"{words[i]} {words[i + 1]} {words[i + 2]}")
    return grams


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def extract_topic_clusters(articles: list[Article], min_articles: int = 3, min_sources: int = 2) -> list[dict]:
    """Group articles that share a distinctive bigram/trigram in their title.

    Returns clusters sorted by score (articles x sources): phrase, label, articles,
    article_count, source_count, score. Overlapping clusters (>= 70% same articles) are merged.
    """
    articles = [a for a in articles if a.kind == "article"]
    ngram_to_arts: dict[str, list[Article]] = defaultdict(list)
    for a in articles:
        for g in title_ngrams(a.title):
            ngram_to_arts[g].append(a)

    candidates = []
    for phrase, arts in ngram_to_arts.items():
        if phrase in GENERIC_NGRAMS or len(arts) < min_articles:
            continue
        sources = {a.source for a in arts}
        if len(sources) < min_sources:
            continue
        candidates.append({
            "phrase": phrase, "label": phrase.title(), "articles": arts,
            "article_count": len(arts), "source_count": len(sources),
            "score": len(arts) * len(sources),
        })
    candidates.sort(key=lambda c: (-c["score"], -len(c["phrase"])))

    merged: list[dict] = []
    for c in candidates:
        urls = {a.url for a in c["articles"]}
        for existing in merged:
            existing_urls = {a.url for a in existing["articles"]}
            union = urls | existing_urls
            if union and len(urls & existing_urls) / len(union) >= 0.7:
                if len(c["phrase"]) > len(existing["phrase"]):
                    existing["phrase"] = c["phrase"]
                    existing["label"] = c["phrase"].title()
                break
        else:
            merged.append(c)
    merged.sort(key=lambda c: -c["score"])
    logging.info(f"Topic clusters: {len(merged)} from {len(articles)} articles")
    return merged


_LABEL_BLOCKLIST = {"fashion", "luxury", "luxe", "mode", "news", "update", "latest", "new", "brand", "collection"}


async def name_topic_clusters(clusters: list[dict], client, model: str) -> list[dict]:
    """Ask Groq for a clean label per cluster (one batched call). Keeps n-gram labels on failure."""
    if not clusters or client is None:
        return clusters
    top = clusters[:12]
    items = [{"id": i, "phrase": c["phrase"], "titles": [a.title for a in c["articles"][:3]]} for i, c in enumerate(top)]
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "Tu nommes des sujets d'actualité mode et luxe. Pour chaque item, donne un label court "
                    "(2 à 4 mots, en français ou nom propre) qui identifie précisément le sujet — par exemple "
                    "'Nomination Demna chez Gucci', 'Tarifs douaniers luxe', 'Défilé Chanel Grand Palais'. "
                    "Évite les mots génériques : Mode, Luxe, Fashion, Actualité. "
                    'Réponds uniquement avec un objet JSON {"labels": [{"id": 0, "label": "..."}]}.')},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
            temperature=0.1, max_tokens=800, **llm_kwargs(model),
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        for entry in data.get("labels", []):
            i, label = entry.get("id"), (entry.get("label") or "").strip()
            if isinstance(i, int) and 0 <= i < len(top) and label and label.lower() not in _LABEL_BLOCKLIST:
                top[i]["label"] = label
    except Exception as e:
        logging.warning(f"Cluster naming failed, keeping n-gram labels: {e}")
    return clusters


# ---------------------------------------------------------------------------
# Story tracking across days
# ---------------------------------------------------------------------------

def cluster_keywords(cluster: dict, limit: int = 20) -> list[str]:
    """Distinctive n-grams of a cluster: its phrase + n-grams shared by >= 2 of its articles."""
    counts: Counter = Counter()
    for a in cluster["articles"]:
        counts.update(title_ngrams(a.title))
    shared = [g for g, n in counts.most_common() if n >= 2 and g not in GENERIC_NGRAMS]
    return list(dict.fromkeys([cluster["phrase"], *shared]))[:limit]


def match_clusters_to_stories(clusters: list[dict], open_stories: list[dict]) -> list[dict | None]:
    """For each cluster, the open story it continues (n-gram overlap) or None. One story per cluster."""
    matches: list[dict | None] = [None] * len(clusters)
    used: set = set()
    scored = []
    for ci, c in enumerate(clusters):
        ckw = set(cluster_keywords(c))
        for s in open_stories:
            skw = set(s.get("keywords") or [])
            overlap = len(ckw & skw)
            if c["phrase"] in skw or overlap >= 2:
                scored.append((overlap + (2 if c["phrase"] in skw else 0), ci, s))
    for _, ci, s in sorted(scored, key=lambda t: -t[0]):
        if matches[ci] is None and s["id"] not in used:
            matches[ci] = s
            used.add(s["id"])
    return matches


def apply_story_matches(store, clusters: list[dict], matches: list[dict | None], today: str) -> dict[str, int]:
    """Create/update stories in the store. Returns {article_url: story_id}."""
    url_to_story: dict[str, int] = {}
    for c, story in zip(clusters, matches):
        keywords = cluster_keywords(c)
        if story:
            merged_kw = list(dict.fromkeys([*keywords, *(story.get("keywords") or [])]))[:40]
            story_id = store.save_story({
                "id": story["id"], "label": story["label"], "first_seen": story["first_seen"],
                "last_seen": today, "article_count": story["article_count"] + c["article_count"],
                "status": "open", "keywords": merged_kw,
            })
        else:
            story_id = store.save_story({
                "label": c["label"], "first_seen": today, "last_seen": today,
                "article_count": c["article_count"], "status": "open", "keywords": keywords,
            })
        for a in c["articles"]:
            url_to_story[a.url] = story_id
    return url_to_story


# ---------------------------------------------------------------------------
# Term counts (word cloud + weak signals on emerging terms)
# ---------------------------------------------------------------------------

def term_counts(articles: list[Article], exclude: set[str], min_articles: int = 2, min_sources: int = 2) -> dict[str, dict]:
    """Per-term stats over `articles`: number of distinct articles and sources.

    Terms are content unigrams (>= 4 chars) and adjacent bigrams; tokens that belong to tracked
    entity names are excluded (entities have their own series). A term must appear in >= `min_articles`
    articles from >= `min_sources` distinct sources (a single outlet repeating itself is not a topic).
    """
    per_term: dict[str, list[Article]] = defaultdict(list)
    for a in articles:
        words = tokens(a.title)
        grams = {w for w in words if len(w) >= 4 and w not in exclude}
        grams |= {f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)
                  if words[i] not in exclude and words[i + 1] not in exclude}
        for g in grams:
            per_term[g].append(a)
    out = {}
    for term, arts in per_term.items():
        n_sources = len({a.source for a in arts})
        if len(arts) >= min_articles and n_sources >= min_sources:
            out[term] = {"count": len(arts), "sources": n_sources}
    return out
