# Luxury Radar — notes pour Claude

Veille mode & luxe (clone adapté de Veille-IA). Voir `README.md` pour l'usage et la méthode.

## Conventions
- Code et commentaires en anglais ; documentation, e-mails, dashboard et prompts en français.
- Python 3.12 en CI (3.11 en local OK). Pas de framework : modules à plat à la racine.
- `python -m pytest` doit rester vert et hors réseau ; les tests réseau portent `@pytest.mark.live`.
- Ajouter une maison / une source = éditer `entities.json` / `sources.json`, pas le code.
- Les 10 catégories vivent dans `models.py` (`CATEGORIES`, `CATEGORY_NOTES`, emojis) ; le prompt Groq, le repli par règles
  (`classify._CATEGORY_RULES`) et le dashboard s'en servent : changer une catégorie = mettre les trois à jour.
- `dashboard_template.html` duplique `topics.tokens()` en JavaScript (nuage de mots) : garder les deux synchronisés.

## Pièges connus
- Le texte est normalisé (accents retirés) avant les regex de `classify.py` : les motifs le sont aussi.
- Les séries d'articles ne comptent qu'à partir du lendemain de la première collecte (`signals.coverage_start`).
- `Store` : Supabase si `SUPABASE_URL`/`SUPABASE_KEY`, sinon SQLite (`LOCAL_DB`, défaut `data/local.db`). Les lectures
  Supabase paginent sur une clé d'ordre unique ; les upserts sont dédoublonnés (Postgres refuse deux fois la même clé).
- `reasoning_effort` n'est envoyé qu'aux modèles qui le supportent (`classify.llm_kwargs`).
- Ne jamais mettre de clé dans le dépôt : `.env` est ignoré, les secrets passent par GitHub Actions.
