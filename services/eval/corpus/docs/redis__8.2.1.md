# Redis 8.2.1

## SET with expiry

Pass EX with seconds: SET session:1 abc EX 3600. Redis deletes the key after the timeout; use PX for milliseconds, or EXAT/PXAT for an absolute Unix timestamp.

```sh
SET session:1 abc EX 3600
TTL session:1   # seconds remaining
```

## What's new in Redis 8: query engine

Redis 8 brings the query engine into Redis Community Edition: FT.CREATE builds secondary indexes over hashes and JSON documents for full-text, numeric, tag, and vector search. Previously this required the separate Redis Stack distribution.

```sh
FT.CREATE userIdx ON HASH PREFIX 1 user: SCHEMA name TEXT age NUMERIC
FT.SEARCH userIdx "@name:alice @age:[20 30]"
```

## SET command

The signature is SET key value [EX seconds | PX milliseconds | EXAT unix-time-seconds | PXAT unix-time-milliseconds | KEEPTTL] [NX | XX] [GET]. NX sets only when the key is absent; XX sets only when it exists.

## Persistence

RDB snapshots the dataset on disk at intervals; AOF logs every write for finer durability. Enable both for safety: save points control RDB frequency, and appendfsync everysec balances AOF durability with speed.

## Pub/Sub

Publish with PUBLISH channel message and subscribe with SUBSCRIBE channel. Messages are fire-and-forget — subscribers that are offline miss them. For durable delivery use Streams (XADD/XREAD) instead.
