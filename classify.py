"""Article classification: category, sentiment, relevance, summary (Groq) with a rule-based fallback."""

import asyncio
import json
import logging
import os
import re

from entities import normalize
from models import CATEGORIES, CATEGORY_NOTES, DEFAULT_CATEGORY, SENTIMENTS, Article

BATCH_SIZE = 15
BATCH_PAUSE = 4.0


def llm_kwargs(model: str) -> dict:
    """Extra chat-completion arguments: only the reasoning models accept `reasoning_effort`."""
    return {"reasoning_effort": "low"} if re.search(r"gpt-oss|qwen3", model or "") else {}


def _system_prompt() -> str:
    notes = "\n".join(f'  - "{c}" : {CATEGORY_NOTES[c]}' for c in CATEGORIES)
    return (
        "Tu es un analyste de veille mode et luxe. On te donne une liste d'articles (titre, source, description). "
        "Pour chacun, renvoie un objet avec :\n"
        f"- \"category\": une valeur parmi {json.dumps(CATEGORIES, ensure_ascii=False)}\n{notes}\n"
        '- "sentiment": "Positif", "Négatif" ou "Neutre" — le ton de la nouvelle pour les maisons/marques concernées '
        "(un bon résultat, un succès, une nomination saluée = Positif ; un scandale, un recul, une polémique = Négatif ; "
        "factuel = Neutre).\n"
        '- "relevant": false si l\'article n\'a AUCUN lien avec la mode, le luxe, la beauté de luxe, l\'horlogerie, la '
        "joaillerie ou les maisons de couture (ex : politique, sport, recette de cuisine) ; sinon true.\n"
        '- "summary": si "needs_summary" vaut true, une phrase en français (20-30 mots) qui donne le fait principal ; sinon "".\n'
        'Réponds UNIQUEMENT avec un objet JSON {"items": [{"id": <id>, "category": ..., "sentiment": ..., '
        '"relevant": ..., "summary": ...}]} contenant un élément par article reçu, sans texte autour.'
    )


def _user_payload(batch: list[Article]) -> str:
    return json.dumps([
        {"id": i, "title": a.title, "source": a.source, "description": a.description[:200],
         "needs_summary": a.hot_topic}
        for i, a in enumerate(batch)
    ], ensure_ascii=False)


# ---------------------------------------------------------------------------
# Rule-based fallback (no API key, or Groq failure)
# ---------------------------------------------------------------------------

_CATEGORY_RULES = [
    ("Ouverture de boutique / pop-up", r"pop-?up|flagship|boutique|ouvre|ouverture|opens? (?:a |its |new )?(?:store|shop)|new store|rénov|renovat|corner"),
    ("Nominations & Créatifs", r"nomm|appoint|named|new creative director|directeur artistique|directrice artistique|creative director|quitte|steps down|départ|successor|succède|takes over|remplace"),
    ("Drama & Controverses", r"polémique|controvers|scandal|scandale|backlash|lawsuit|procès|boycott|accus|accused|racis|contrefaçon|counterfeit|fraud|bad buzz|outrage|criticis|critiqu"),
    ("RSE", r"durable|sustainab|rse\b|esg|éthique|ethic|recycl|seconde main|secondhand|resale|carbon|carbone|climat|climate|biodivers|fur\b|fourrure|animal welfare|diversity|diversité|inclusi|greenwash"),
    ("Business & Finance", r"revenue|sales|profit|earnings|shares|stock|results|acquisition|acquire|acquires|merger|investor|invest|chiffre d.affaires|résultats|bourse|rachat|rachète|actionnaire|dividend|guidance|valuation|cède|stake|croissance|growth|financ"),
    ("Marchés & Consommateurs", r"china|chine|asia|asie|middle east|moyen-orient|india|inde|japan|japon|tariff|droits de douane|douane|consumer|consommateur|demand|demande|gen z|millennial|tourist|touriste|market share|marché"),
    ("Tech & Retail", r"\bai\b|\bia\b|artificial intelligence|intelligence artificielle|e-?commerce|digital|online|app\b|retail|nft|blockchain|metaverse|omnicanal|platform|plateforme|tech"),
    ("Collections & Défilés", r"fashion week|semaine de la mode|défilé|runway|collection|couture|ready-to-wear|prêt-à-porter|show\b|spring|summer|autumn|winter|printemps|été|automne|hiver|campaign|campagne|lookbook|capsule|sneaker|baskets"),
    ("Culture & Célébrités", r"celebrit|célébrité|actress|actrice|actor|acteur|singer|chanteu|met gala|red carpet|tapis rouge|ambassad|muse\b|star\b|expo|museum|musée|film|cinema|cinéma|festival|album|art\b"),
]

_POSITIVE = r"record|success|succès|croissance|growth|boost|rebond|rebound|surge|soar|win|award|prix\b|acclaim|saluée?|triumph|love|iconic|hit\b|sold out|beat|hausse|gagne|ouvre|celebrat|innov|renaissance|revival|comeback"
_NEGATIVE = r"drop|fall|falls|plunge|slump|decline|chute|baisse|recul|crise|crisis|scandal|scandale|lawsuit|procès|boycott|backlash|polémique|controvers|accus|fraud|layoff|licenci|closure|ferme|weak|faible|warning|alerte|cut|cuts|loss|perte|profit warning|flop|fail|tariff|douane|counterfeit|contrefaçon|racis"


# The text is accent-stripped before matching, so the patterns must be too ('polémique' -> 'polemique').
_CATEGORY_RULES = [(cat, re.compile(normalize(pattern))) for cat, pattern in _CATEGORY_RULES]
_POSITIVE, _NEGATIVE = re.compile(normalize(_POSITIVE)), re.compile(normalize(_NEGATIVE))


def fallback_classify(article: Article) -> tuple[str, str]:
    text = normalize(f"{article.title} {article.description}")
    category = DEFAULT_CATEGORY
    for cat, pattern in _CATEGORY_RULES:
        if pattern.search(text):
            category = cat
            break
    pos, neg = len(_POSITIVE.findall(text)), len(_NEGATIVE.findall(text))
    sentiment = "Positif" if pos > neg else "Négatif" if neg > pos else "Neutre"
    return category, sentiment


def apply_fallback(article: Article) -> None:
    article.category, article.sentiment = fallback_classify(article)


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------

def _apply_item(article: Article, item: dict) -> None:
    cat, sent = item.get("category"), item.get("sentiment")
    if cat not in CATEGORIES or sent not in SENTIMENTS:
        fb_cat, fb_sent = fallback_classify(article)
        cat = cat if cat in CATEGORIES else fb_cat
        sent = sent if sent in SENTIMENTS else fb_sent
    article.category, article.sentiment = cat, sent
    article.relevant = item.get("relevant", True) is not False
    if article.hot_topic:
        article.summary = (item.get("summary") or "").strip()


async def _classify_batch(client, model: str, batch: list[Article]) -> bool:
    """Classify one batch in-place. Returns False (and applies the fallback) on any failure."""
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _system_prompt()}, {"role": "user", "content": _user_payload(batch)}],
            temperature=0.1, max_tokens=4000, **llm_kwargs(model),
            response_format={"type": "json_object"},
        )
        items = json.loads(resp.choices[0].message.content).get("items", [])
        by_id = {it.get("id"): it for it in items if isinstance(it, dict)}
        if not by_id:
            raise ValueError("no items in response")
        missing = 0
        for i, article in enumerate(batch):
            if i in by_id:
                _apply_item(article, by_id[i])
            else:
                apply_fallback(article)
                missing += 1
        if missing:
            logging.warning(f"Groq omitted {missing}/{len(batch)} items in a batch - fallback used for those")
        return True
    except Exception as e:
        logging.warning(f"Groq batch failed ({len(batch)} articles): {e}")
        for article in batch:
            apply_fallback(article)
        return False


def make_client():
    """AsyncGroq client, or None when GROQ_API_KEY is not set."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    from groq import AsyncGroq
    return AsyncGroq(api_key=key)


def groq_model() -> str:
    return (os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b").strip().strip("'\"").strip()


async def classify_articles(articles: list[Article], client=None, model: str | None = None,
                            batch_size: int = BATCH_SIZE, pause: float = BATCH_PAUSE) -> list[Article]:
    """Classify all articles in-place; drops those the model flags as not relevant (filtered sources only)."""
    if client is None:
        logging.warning("GROQ_API_KEY not set - using the rule-based classifier")
        for a in articles:
            apply_fallback(a)
        return articles

    model = model or groq_model()
    batches = [articles[i:i + batch_size] for i in range(0, len(articles), batch_size)]
    failures = 0
    for i, batch in enumerate(batches):
        logging.info(f"Groq: batch {i + 1}/{len(batches)} ({len(batch)} articles)")
        if not await _classify_batch(client, model, batch):
            failures += 1
        if i < len(batches) - 1:
            await asyncio.sleep(pause)

    if batches and failures / len(batches) > 0.2:
        msg = (f"Groq failed on {failures}/{len(batches)} batches - check GROQ_API_KEY and the GROQ_MODEL "
               f"Actions variable (a decommissioned model name overrides the code default).")
        logging.error(msg)
        print(f"::error::{msg}")

    before = len(articles)
    kept = [a for a in articles if a.relevant or not a.filter]
    if len(kept) < before:
        logging.info(f"Groq relevance filter dropped {before - len(kept)} off-topic items from generalist sources")
    return kept
