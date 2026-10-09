# PostgreSQL 18.0

## Partial indexes

Add a WHERE clause: CREATE INDEX active_users ON users (email) WHERE active;. A partial index covers only rows matching the predicate, keeping it smaller and faster for selective queries.

```sql
CREATE INDEX orders_unshipped_idx
    ON orders (created_at)
    WHERE shipped_at IS NULL;
```

## string_agg

The signature is string_agg(expression, delimiter) with an optional ORDER BY inside the aggregate: string_agg(expression, delimiter ORDER BY sort_expression). It concatenates the input values into a single string.

```sql
SELECT dept, string_agg(name, ', ' ORDER BY name) AS members
FROM employees
GROUP BY dept;
```

## EXPLAIN

EXPLAIN ANALYZE runs the query and shows actual row counts and timings per node. Look for Seq Scan on large tables (add an index), high "Rows Removed by Filter" (the filter isn't selective), and misestimated rows (run ANALYZE).

## JSONB

Store documents in jsonb: CREATE TABLE events (payload jsonb). Query with -> and ->>: payload->>'name' returns text. Index paths with GIN: CREATE INDEX ON events USING gin (payload).
