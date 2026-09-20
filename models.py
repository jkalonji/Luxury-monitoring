"""Shared data model and constants for the Luxury Radar pipeline."""

from dataclasses import dataclass, field
from datetime import datetime, timezone

CATEGORIES = [
    "Business & Finance",
    "Collections & Défilés",
    "Nominations & Créatifs",
    "Marchés & Consommateurs",
    "RSE",
    "Tech & Retail",
    "Culture & Célébrités",
    "Drama & Controverses",
    "Ouverture de boutique / pop-up",
    "Actualités des Maisons de luxe",
]
DEFAULT_CATEGORY = "Actualités des Maisons de luxe"

CATEGORY_EMOJI = {
    "Business & Finance": "💼",
    "Collections & Défilés": "👗",
    "Nominations & Créatifs": "🎨",
    "Marchés & Consommateurs": "🌏",
    "RSE": "🌱",
    "Tech & Retail": "📱",
    "Culture & Célébrités": "⭐",
    "Drama & Controverses": "💥",
    "Ouverture de boutique / pop-up": "🏬",
    "Actualités des Maisons de luxe": "🏛️",
}

SENTIMENTS = ("Positif", "Négatif", "Neutre")

# Classification guidance shared by the Groq prompt and the docs.
CATEGORY_NOTES = {
    "Business & Finance": "résultats financiers, chiffre d'affaires, bourse, fusions-acquisitions, investissements, stratégie de groupe",
    "Collections & Défilés": "fashion weeks, défilés, collections, campagnes, lancements produits, haute couture",
    "Nominations & Créatifs": "nominations et départs de directeurs artistiques, PDG, créatifs, collaborations créatives",
    "Marchés & Consommateurs": "demande par pays/région (Chine, USA, Moyen-Orient, Inde...), comportement des clients, tarifs douaniers, tourisme d'achat",
    "RSE": "développement durable, éthique, matières, seconde main/recyclage, conditions de travail, diversité, gouvernance",
    "Tech & Retail": "e-commerce, IA, digital, omnicanal, retail, seconde main en ligne, blockchain, expérience client",
    "Culture & Célébrités": "célébrités, ambassadeurs, Met Gala, red carpet, art, musique, cinéma, expositions",
    "Drama & Controverses": "polémiques, scandales, procès, boycotts, accusations, contrefaçon, bad buzz",
    "Ouverture de boutique / pop-up": "ouvertures/fermetures de boutiques, flagships, pop-ups, rénovations, corners",
    "Actualités des Maisons de luxe": "actualité générale d'une maison de luxe qui ne rentre dans aucune autre catégorie (anniversaires, patrimoine, savoir-faire, produits iconiques)",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_str() -> str:
    return now_utc().strftime("%Y-%m-%d")


@dataclass
class Article:
    title: str
    url: str
    source: str
    country: str
    published: str                      # ISO 8601 datetime (UTC)
    description: str = ""
    kind: str = "article"               # "article" (magazine) | "social" (Bluesky, Reddit)
    lang: str = "en"
    category: str = ""
    sentiment: str = ""
    entities: list[str] = field(default_factory=list)  # ids from entities.json
    engagement: int = 0                 # social posts only (likes + 3*reposts + 2*replies)
    hot_topic: bool = False
    hot_reason: str = ""                # cluster label
    mention_count: int = 0              # size of the cluster this article belongs to
    supa_hot: bool = False
    summary: str = ""
    story_id: int | None = None
    published_is_estimated: bool = False
    filter: str = ""                    # source filter ("luxury") — not persisted
    relevant: bool = True               # set by classifier for filtered sources — not persisted

    @property
    def day(self) -> str:
        return self.published[:10]
