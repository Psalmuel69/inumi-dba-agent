-- Seed data for the `postgres-sample` dummy database (docker-compose).
-- Just enough schema + rows that the read-only diagnostic tools
-- (get_tables, get_indexes, get_statistics, get_storage, ...) return
-- something interesting when the agent investigates it.

CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.transaction_posting_history (
    id              bigserial PRIMARY KEY,
    account_id      bigint      NOT NULL,
    posted_at       timestamptz NOT NULL DEFAULT now(),
    amount_minor    bigint      NOT NULL,
    currency        text        NOT NULL DEFAULT 'NGN',
    description     text
);

CREATE INDEX IF NOT EXISTS ix_tph_account_id ON core.transaction_posting_history (account_id);
CREATE INDEX IF NOT EXISTS ix_tph_posted_at  ON core.transaction_posting_history (posted_at);

INSERT INTO core.transaction_posting_history (account_id, amount_minor, description)
SELECT (random() * 10000)::bigint,
       ((random() - 0.5) * 5_000_00)::bigint,
       'seed row ' || g
FROM generate_series(1, 25_000) AS g;

ANALYZE core.transaction_posting_history;

-- pg_stat_statements is what get_top_queries / get_query_plan read.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
