import json

from entities import EntityIndex, normalize


def test_normalize_strips_accents_and_case():
    assert normalize("Alaïa Ghesquière") == "alaia ghesquiere"


def test_tag_whole_word_and_accent_insensitive(index):
    assert index.tag("ALAIA's new store") == ["alaia"]
    assert index.tag("Alaïa opens") == ["alaia"]
    assert set(index.tag("Christian Dior and Gucci")) == {"dior", "gucci"}


def test_tag_does_not_match_inside_words(index):
    assert index.tag("Dioramas and Guccione") == []


def test_disabled_entities_are_ignored(index):
    assert "off" not in index.by_id
    assert index.tag("Off Person spotted") == []


def test_is_luxury_rules(index):
    assert index.is_luxury("Gucci results")                      # tracked entity
    assert index.is_luxury("Le marché du luxe en Chine")         # strong keyword
    assert not index.is_luxury("Le Luxembourg vote un budget")   # 'luxe' must not match 'Luxembourg'
    assert not index.is_luxury("A new fashion podcast")          # one weak keyword is not enough
    assert index.is_luxury("A new fashion brand launches")       # two weak keywords


def test_alias_words_and_label(index):
    words = index.alias_words()
    assert {"christian", "dior", "moet", "hennessy"} <= words
    assert index.label("dior") == "Dior"
    assert index.label("unknown") == "unknown"


def test_real_entities_file_is_consistent():
    idx = EntityIndex.load("entities.json")
    ids = [e.id for e in idx.entities]
    assert len(ids) == len(set(ids)), "duplicate entity ids"
    houses = {e.label for e in idx.entities if e.type == "house"}
    for name in ["Dior", "Louis Vuitton", "Balenciaga", "Bottega Veneta", "Fendi", "Gucci", "Burberry", "Jil Sander",
                 "Versace", "Kenzo", "Saint Laurent", "Miu Miu", "Coperni", "Valentino", "Givenchy",
                 "Vivienne Westwood", "Chanel", "Prada", "Loewe", "JW Anderson", "Ralph Lauren", "Alaïa"]:
        assert name in houses, name
    groups = {e.label for e in idx.entities if e.type == "group"}
    assert {"LVMH", "Kering", "Richemont"} <= groups
    assert idx.tag("Miu Miu and Prada at Milan") and "miu_miu" in idx.tag("Miu Miu show")
    assert "prada" not in idx.tag("Miu Miu show")
    assert "jil_sander" in idx.tag("Jil Sander's new collection")
    for e in idx.entities:
        assert e.aliases and e.trends


def test_real_entities_no_ambiguous_alias():
    """Every alias of an entity must tag that entity."""
    idx = EntityIndex.load("entities.json")
    for e in idx.entities:
        for alias in e.aliases:
            assert e.id in idx.tag(f"News about {alias} today"), (e.id, alias)
