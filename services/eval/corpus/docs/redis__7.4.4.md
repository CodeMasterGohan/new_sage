# Redis 7.4.4

## SADD

Call SADD key member [member ...]: it adds the members to the set stored at key and returns the number of members actually added. Adding an existing member is a no-op.

```sh
SADD myset "a" "b" "c"   # (integer) 3
SADD myset "a"           # (integer) 0
SMEMBERS myset
```

## ZADD

The signature is ZADD key [NX | XX] [GT | LT] [CH] [INCR] score member [score member ...]. It adds members with scores to the sorted set, updating scores of existing members.

## Hashes

HSET user:1 name alice age 30 stores fields; HGET user:1 name reads one field, HGETALL user:1 reads everything. Hashes are the idiomatic way to model objects without serializing JSON.

## Lua scripting

EVAL "return redis.call('get', KEYS[1])" 1 mykey runs Lua atomically on the server. Scripts execute atomically — keep them fast. Redis 7.0 added Redis Functions as the managed alternative to ad-hoc EVAL scripts.
