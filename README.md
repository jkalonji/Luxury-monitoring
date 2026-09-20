# Luxury Radar — veille mode & luxe

Radar quotidien de l'actualité des maisons de mode et de luxe : collecte de 24 magazines, classification (catégorie,
sentiment), détection de sujets chauds et de **signaux faibles**, récap par e-mail (quotidien + hebdomadaire) et
dashboard publié sur GitHub Pages.

```
GitHub Actions (cron)
  └─ main.py ── collecte (RSS / Google News / Reddit / Bluesky)
              ├─ classification (Groq, repli par règles)
              ├─ sujets chauds + histoires multi-jours
              ├─ séries de signaux (articles, Wikipedia, Google Trends, social)
              ├─ stockage (Supabase, ou SQLite en local)
              └─ e-mail quotidien (Resend)
  └─ dashboard.py --export ──► site/index.html ──► GitHub Pages
  └─ weekly_report.py (lundi) ──► e-mail hebdomadaire
```

## Ce que contient le dashboard

| Section | Contenu |
|---|---|
| **Signaux faibles** | Maisons / personnes / termes dont une métrique a au moins doublé la veille (voir « Méthode »), avec courbe sur 30 jours |
| **Sentiment** | Évolution quotidienne (positif / neutre / négatif) et tableau par maison ou personne |
| **Nuage de mots** | Termes les plus fréquents des titres, colorés selon le sentiment moyen ; clic = filtre |
| **Qui en parle ?** | Choisir un sujet (maison, personne, terme) : sources classées par nombre d'articles, avec leur sentiment |
| **Sujets chauds** | Sujets traités par ≥ 3 articles de ≥ 2 sources, et histoires qui durent plusieurs jours |
| **Catégories / Articles** | Les 10 catégories et la liste filtrable des articles |

Filtres : période (7 / 14 / 30 jours), maison. Thème clair / sombre automatique.

## Sources (24 magazines)

Définies dans [`sources.json`](sources.json). Trois modes :

- `rss` : flux de la rubrique mode. Si le flux échoue (403, vide…), repli automatique sur une recherche Google News `site:`.
- `gnews` : recherche Google News RSS (magazines sans flux exploitable).
- `reddit` : flux RSS de subreddits mode (contexte social, sans score d'engagement).

Seuls les **titres et extraits** sont lus : aucun article payant n'est récupéré. Les sources généralistes (Elle, GQ, NYT,
Le Monde, Madame Figaro, Tatler, Harper's Bazaar…) ont `"filter": "luxury"` : seuls les articles mentionnant une maison
suivie ou du vocabulaire luxe/mode sont gardés, puis Groq écarte les hors-sujet.

## Entités suivies

[`entities.json`](entities.json) : 22 maisons, 3 groupes (LVMH, Kering, Richemont), une liste de personnes (créateurs,
dirigeants) et les mots-clés de pertinence. Chaque entité a ses alias (détection insensible aux accents et à la casse,
mots entiers), ses pages Wikipedia et son terme Google Trends. Pour ajouter une maison, ajouter un bloc dans `houses` :
aucun code à toucher. **La liste `people` est une proposition à valider** (`"enabled": false` pour en retirer).

## Catégories

Business & Finance · Collections & Défilés · Nominations & Créatifs · Marchés & Consommateurs · RSE ·
Tech & Retail · Culture & Célébrités · Drama & Controverses · Ouverture de boutique / pop-up ·
Actualités des Maisons de luxe (catégorie par défaut).

## Méthode : signaux faibles (seuils simples)

Chaque jour, pour chaque entité, on stocke des séries temporelles (`signals`) :

| Métrique | Source | Plancher (valeur du jour) |
|---|---|---|
| `articles` | articles de magazines citant l'entité (ou le terme) | ≥ 3 |
| `wiki_views` | pages vues Wikipedia (toutes langues configurées) | ≥ 100 |
| `trends` | Google Trends (0-100, relatif) | ≥ 15 |
| `social_engagement` | likes + 3×reposts + 2×réponses des posts Bluesky | ≥ 100 |
| `social_posts` | nombre de posts (contexte, ne déclenche pas seul) | — |

Un **signal faible** se déclenche sur la dernière journée complète (J‑1) quand la valeur est **≥ 2 × la moyenne des
7 jours précédents** (avec un plancher sur la moyenne pour éviter les ratios infinis) et dépasse le plancher ci-dessus.
Plusieurs métriques qui convergent sur la même entité augmentent son score. Les termes émergents (mots des titres traités
par ≥ 2 sources) suivent la même règle.

Garde-fous : au moins 3 jours d'historique par métrique ; les séries d'articles ne démarrent que le **lendemain de la
première collecte** (les flux RSS ne remontent que quelques jours : un backfill fausserait la moyenne). Les seuils sont
dans `signals.py` (`THRESHOLDS`).

## Mise en route

### 1. Comptes et secrets

| Service | À faire | Secret / variable GitHub |
|---|---|---|
| **Supabase** (gratuit) | Créer un projet, exécuter [`schema.sql`](schema.sql) dans *SQL Editor* | `SUPABASE_URL`, `SUPABASE_KEY` (clé **service_role**, jamais dans le navigateur) |
| **Groq** | Créer une clé API | `GROQ_API_KEY` (+ variable `GROQ_MODEL`, défaut `openai/gpt-oss-120b`) |
| **Resend** | Créer une clé API ; idéalement vérifier un domaine d'envoi | `RESEND_API_KEY`, `MAIL_TO` (destinataires séparés par des virgules), variable `MAIL_FROM` |
| **Bluesky** (optionnel) | Créer un *app password* (Réglages → Mots de passe d'application) | `BSKY_HANDLE`, `BSKY_APP_PASSWORD` |

Sans domaine vérifié, Resend n'envoie qu'à l'adresse du compte, depuis `onboarding@resend.dev`.
Sans `GROQ_API_KEY` le pipeline fonctionne avec un classifieur par règles (moins précis). **Sans Supabase, rien n'est
conservé entre deux exécutions** (le runner GitHub est éphémère) : l'historique des signaux ne se construit pas.

Réglages GitHub : *Settings → Secrets and variables → Actions* pour les secrets ; *Settings → Pages → Source :
GitHub Actions* pour le dashboard.

### 2. Premier lancement

1. *Actions → Luxury Radar - Daily collection → Run workflow* avec `backfill_days = 14` (charge l'historique, pas d'e-mail).
2. Les jours suivants, le cron de 07:00 UTC fait le reste. Les signaux basés sur le volume d'articles apparaissent
   après ~4 jours de collecte ; Wikipedia et Google Trends dès le premier jour (historique fourni par ces services).
3. Le dashboard est publié à `https://<utilisateur>.github.io/Luxury-monitoring/`.

### 3. En local

```bash
pip install -r requirements-dev.txt
cp .env.example .env            # renseigner ce qu'on a ; tout est optionnel en local
python main.py --dry-run        # collecte + analyse, aperçu du mail dans output/daily.html
python weekly_report.py --dry-run
python dashboard.py --export --output site/index.html   # ou : streamlit run dashboard.py
python -m pytest                # tests hors réseau
python -m pytest -m live        # tests contre les vrais services
```

Sans `SUPABASE_*`, les données vont dans `data/local.db` (SQLite, ignoré par git).

## Dépannage

Premier réflexe : *Actions → Luxury Radar - Check setup → Run workflow*. Ce job vérifie en une minute, sans afficher
aucun secret, que Supabase (clé `service_role`, bon projet, tables présentes), Groq (clé et modèle), Resend (clé, destinataires,
domaine d'envoi vérifié) et Bluesky acceptent vos réglages.

| Message dans le log | Cause | Solution |
|---|---|---|
| `Could not find the table 'public.articles'` (`PGRST205`) | `schema.sql` n'a pas été exécuté sur le projet Supabase pointé par `SUPABASE_URL` | Supabase → *SQL Editor* → coller `schema.sql` → *Run*. Si l'erreur persiste : `NOTIFY pgrst, 'reload schema';` |
| `401` / `Invalid API key` | `SUPABASE_KEY` incorrecte | Utiliser la clé **service_role** (Project Settings → API), pas la clé anon |
| `Resend rejected the e-mail (403)` | domaine d'envoi non vérifié | Vérifier un domaine dans Resend, ou n'envoyer qu'à l'adresse du compte avec `onboarding@resend.dev` |
| `Groq failed on N batches` | clé invalide ou modèle retiré | Vérifier `GROQ_API_KEY` et la variable Actions `GROQ_MODEL` |

## Workflows

| Fichier | Déclencheur | Rôle |
|---|---|---|
| `daily.yml` | tous les jours 07:00 UTC + manuel | collecte, e-mail quotidien, export et déploiement du dashboard |
| `weekly.yml` | lundi 07:30 UTC + manuel | e-mail hebdomadaire (7 jours vs 7 précédents) |
| `dashboard.yml` | manuel | reconstruit et publie le dashboard sans collecter |
| `check.yml` | manuel | vérifie les secrets et les services (voir Dépannage) |
| `ci.yml` | push / PR | tests |

## Limites connues

- **Google Trends** (pytrends, non officiel) peut répondre 429 depuis les IP de GitHub : la collecte s'arrête proprement
  et les autres métriques continuent. Les valeurs sont relatives à chaque lot de 5 termes.
- **Reddit** bloque souvent les IP de datacenter et ne fournit pas de score via RSS : source de contexte uniquement.
- **Bluesky** : la recherche exige un compte (identifiants ci-dessus) ; sans eux, la source est ignorée.
- **X / Twitter** : reporté à la v2.
- Le sentiment est celui du **ton de la nouvelle** pour les maisons concernées (jugé par Groq à partir du titre et de
  l'extrait), pas celui des lecteurs.
- Les magazines dont le flux est pauvre (Le Journal du Luxe, Views, V Magazine, Luxury Daily) apportent peu d'articles.

## Structure

```
main.py            pipeline quotidien            weekly_report.py   e-mail hebdo
fetchers.py        RSS / Google News / Reddit / Bluesky
classify.py        Groq + règles de repli        topics.py          clusters, histoires, termes
signals.py         séries + détection            mailer.py          e-mails HTML (Resend)
store.py           Supabase / SQLite             dashboard.py       export + page (dashboard_template.html)
entities.py        alias, filtre luxe            models.py          catégories, dataclass Article
```
