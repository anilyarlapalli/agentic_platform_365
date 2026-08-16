# Outbox stalled

Postgres is the source of truth. Do not delete outbox rows or enqueue copies by
hand.

1. Check relay replica health, `platform_outbox_pending` and oldest age. Inspect
   relay logs by trace/run id, not by tenant content.
2. Verify the relay credential can execute only its documented tables/functions
   and that Redis TLS/authentication succeeds.
3. Restart or roll back the relay if its loop is wedged. Multiple replicas are
   safe because draining uses row locks; published duplicates lose the run
   lease race.
4. Inspect poison rows and fix the deterministic payload/configuration issue.
   Do not mark a row published unless broker receipt is established.
5. Resolve when oldest age is below 30 seconds, depth is draining, and affected
   durable runs have either completed or are visibly retrying.

