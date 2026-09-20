-- Luxury Radar — schéma Supabase (Postgres).
-- À exécuter UNE fois dans Supabase : SQL Editor > New query > coller > Run.

CREATE TABLE IF NOT EXISTS stories (
    id            BIGSERIAL PRIMARY KEY,
    label         TEXT NOT NULL,
    first_seen    DATE NOT NULL,
    last_seen     DATE NOT NULL,
    article_count INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'open',
    keywords      JSONB NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS articles (
    url                    TEXT PRIMARY KEY,
    title                  TEXT NOT NULL,
    source                 TEXT,
    country                TEXT,
    published              TIMESTAMPTZ NOT NULL,
    description            TEXT DEFAULT '',
    kind                   TEXT NOT NULL DEFAULT 'article',   -- 'article' (magazine) | 'social' (Bluesky, Reddit)
    lang                   TEXT,
    category               TEXT,
    sentiment              TEXT,                              -- Positif | Négatif | Neutre
    entities               JSONB NOT NULL DEFAULT '[]',       -- ids de entities.json
    engagement             INTEGER NOT NULL DEFAULT 0,        -- posts sociaux : likes + 3*reposts + 2*replies
    hot_topic              BOOLEAN NOT NULL DEFAULT FALSE,
    hot_reason             TEXT DEFAULT '',                   -- label du cluster de sujet
    mention_count          INTEGER NOT NULL DEFAULT 0,        -- taille du cluster
    supa_hot               BOOLEAN NOT NULL DEFAULT FALSE,
    summary                TEXT DEFAULT '',
    story_id               BIGINT REFERENCES stories(id),
    published_is_estimated BOOLEAN NOT NULL DEFAULT FALSE,
    collected_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS articles_published_idx ON articles (published DESC);

-- Séries temporelles quotidiennes pour les signaux faibles.
-- metric : articles | wiki_views | trends | social_engagement | social_posts
-- entity_type : house | group | person | term
CREATE TABLE IF NOT EXISTS signals (
    entity_id   TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    metric      TEXT NOT NULL,
    day         DATE NOT NULL,
    value       DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (entity_id, metric, day)
);
CREATE INDEX IF NOT EXISTS signals_day_idx ON signals (day DESC);

-- Le pipeline utilise la clé service_role (côté GitHub Actions uniquement) : activer RLS sans policy
-- garantit que la clé publique anon ne peut rien lire.
ALTER TABLE stories  ENABLE ROW LEVEL SECURITY;
ALTER TABLE articles ENABLE ROW LEVEL SECURITY;
ALTER TABLE signals  ENABLE ROW LEVEL SECURITY;
