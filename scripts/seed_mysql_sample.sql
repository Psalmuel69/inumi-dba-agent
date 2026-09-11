-- Seed data for the opt-in `mysql-sample` / `mariadb-sample` dummy
-- databases (docker compose --profile mysql|mariadb up -d ...-sample).
-- Just enough schema + rows that the read-only diagnostic tools
-- (get_tables, get_indexes, get_statistics, get_storage, ...) return
-- something interesting when the agent investigates it. Works unmodified
-- on both MySQL 8 and MariaDB 11.

CREATE TABLE IF NOT EXISTS transaction_posting_history (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    account_id    BIGINT       NOT NULL,
    posted_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    amount_minor  BIGINT       NOT NULL,
    currency      VARCHAR(3)   NOT NULL DEFAULT 'NGN',
    description   VARCHAR(200)
) ENGINE=InnoDB;

CREATE INDEX ix_tph_account_id ON transaction_posting_history (account_id);
CREATE INDEX ix_tph_posted_at  ON transaction_posting_history (posted_at);

-- Recursive CTE row generator (MySQL 8.0+ / MariaDB 10.2+) — no
-- procedural loop needed.
INSERT INTO transaction_posting_history (account_id, amount_minor, description)
SELECT (RAND() * 10000), ((RAND() - 0.5) * 500000), CONCAT('seed row ', n)
FROM (
    WITH RECURSIVE seq(n) AS (
        SELECT 1
        UNION ALL
        SELECT n + 1 FROM seq WHERE n < 5000
    )
    SELECT n FROM seq
) AS g;

ANALYZE TABLE transaction_posting_history;
