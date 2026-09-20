"""Tracked entities (houses, groups, people): loading, mention tagging, luxury-relevance filter."""

import json
import re
import unicodedata
from dataclasses import dataclass, field


def normalize(text: str) -> str:
    """Lowercase + strip accents so 'Alaïa' matches 'alaia' and 'Ghesquière' matches 'ghesquiere'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


@dataclass
class Entity:
    id: str
    label: str
    type: str                           # "house" | "group" | "person"
    aliases: list[str]
    group: str = ""
    wikipedia: dict[str, str] = field(default_factory=dict)   # lang -> page title
    trends: str = ""


class EntityIndex:
    def __init__(self, data: dict):
        self.entities: list[Entity] = []
        for key, etype in (("houses", "house"), ("groups", "group"), ("people", "person")):
            for e in data.get(key, []):
                if e.get("enabled", True) is False:
                    continue
                self.entities.append(Entity(
                    id=e["id"], label=e["label"], type=etype,
                    aliases=e.get("aliases") or [e["label"]], group=e.get("group", ""),
                    wikipedia=e.get("wikipedia") or {}, trends=e.get("trends") or e["label"],
                ))
        self.by_id = {e.id: e for e in self.entities}
        # One compiled regex per entity; whole-word, accent/case insensitive.
        self._patterns = {
            e.id: re.compile(r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" % "|".join(
                re.escape(normalize(a)) for a in sorted(e.aliases, key=len, reverse=True)))
            for e in self.entities
        }
        kw = data.get("luxury_keywords", {})
        self._strong = [self._kw_pattern(k) for k in kw.get("strong", [])]
        self._weak = [self._kw_pattern(k) for k in kw.get("weak", [])]
        self._alias_words = {normalize(a) for e in self.entities for a in e.aliases}

    @staticmethod
    def _kw_pattern(keyword: str) -> "re.Pattern":
        """Whole-word keyword regex (plural 's' tolerated) so 'luxe' does not match 'Luxembourg'."""
        return re.compile(r"(?<![a-z0-9])" + re.escape(normalize(keyword)) + r"(?:s|es)?(?![a-z0-9])")

    @classmethod
    def load(cls, path: str = "entities.json") -> "EntityIndex":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def tag(self, text: str) -> list[str]:
        """Return ids of every tracked entity mentioned in `text`."""
        norm = normalize(text)
        return [eid for eid, pat in self._patterns.items() if pat.search(norm)]

    def is_luxury(self, text: str) -> bool:
        """Relevance filter for generalist sources: a tracked entity, one strong keyword, or two weak ones."""
        norm = normalize(text)
        if any(pat.search(norm) for pat in self._patterns.values()):
            return True
        if any(p.search(norm) for p in self._strong):
            return True
        return sum(1 for p in self._weak if p.search(norm)) >= 2

    def alias_words(self) -> set[str]:
        """Normalized single tokens belonging to entity names (excluded from generic term stats)."""
        words: set[str] = set()
        for a in self._alias_words:
            words.update(re.findall(r"[a-z0-9]+", a))
        return words

    def label(self, entity_id: str) -> str:
        e = self.by_id.get(entity_id)
        return e.label if e else entity_id
