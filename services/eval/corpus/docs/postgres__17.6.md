# PostgreSQL 17.6

## psql meta-commands

Run \dt in psql: it lists all tables in the current schema search path. Use \dt *.* to see tables in every schema, and \d tablename to describe one table.

## What's new in PostgreSQL 17: incremental backups

PostgreSQL 17 added incremental base backups: pg_basebackup --incremental takes a backup containing only blocks changed since the previous backup in the chain, shrinking backup size and time.

```sh
pg_basebackup -D /backup/full --checkpoint=fast
pg_basebackup -D /backup/incr1 --incremental=/backup/full/backup_manifest
```

## CREATE INDEX

The signature is CREATE [ UNIQUE ] INDEX [ CONCURRENTLY ] [ [ IF NOT EXISTS ] name ] ON [ ONLY ] table_name [ USING method ] ( { column_name | ( expression ) } [ ... ] ).

## VACUUM

VACUUM reclaims storage from dead tuples; autovacuum runs it automatically. Run VACUUM (VERBOSE, ANALYZE) manually after bulk loads. VACUUM FULL rewrites the table compactly but takes an exclusive lock — avoid it in production hours.

## Transactions

Wrap writes in BEGIN; ... COMMIT; to make them atomic. The default isolation level is read committed; use REPEATABLE READ or SERIALIZABLE when concurrent writes must not interleave. A failed statement aborts the whole transaction — ROLLBACK and retry.
