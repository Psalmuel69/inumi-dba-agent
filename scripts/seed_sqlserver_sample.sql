-- Seed data for the opt-in `mssql-sample` dummy database (docker-compose
-- --profile mssql). Run it once the container is healthy, e.g.:
--
--   docker compose --profile mssql exec mssql-sample /opt/mssql-tools18/bin/sqlcmd \
--     -S localhost -U sa -P 'Str0ng_Passw0rd!' -C -i /seed/seed.sql
--
-- Then add a matching entry to config/dev_credentials.yaml.

IF DB_ID('SampleCoreBanking') IS NULL
    CREATE DATABASE SampleCoreBanking;
GO

USE SampleCoreBanking;
GO

IF OBJECT_ID('dbo.TransactionPostingHistory', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.TransactionPostingHistory (
        Id            BIGINT IDENTITY(1,1) PRIMARY KEY,
        AccountId     BIGINT       NOT NULL,
        PostedAt      DATETIME2    NOT NULL DEFAULT SYSUTCDATETIME(),
        AmountMinor   BIGINT       NOT NULL,
        Currency      NVARCHAR(3)  NOT NULL DEFAULT 'NGN',
        Description   NVARCHAR(200)
    );
    CREATE INDEX IX_TPH_AccountId ON dbo.TransactionPostingHistory (AccountId);
    CREATE INDEX IX_TPH_PostedAt  ON dbo.TransactionPostingHistory (PostedAt);
END
GO

SET NOCOUNT ON;
DECLARE @i INT = 0;
WHILE @i < 25000
BEGIN
    INSERT INTO dbo.TransactionPostingHistory (AccountId, AmountMinor, Description)
    VALUES (ABS(CHECKSUM(NEWID())) % 10000,
            (ABS(CHECKSUM(NEWID())) % 500000) - 250000,
            CONCAT('seed row ', @i));
    SET @i += 1;
END
GO

UPDATE STATISTICS dbo.TransactionPostingHistory;
GO
