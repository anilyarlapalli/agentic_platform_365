# API latency SLO burn

1. Split p95 by route and release, then follow slow traces into SQL, Redis,
   object storage, retrieval and model spans.
2. Check database statement cancellations, pool wait, provider throttling and
   worker queue depth. A model call obeying `Retry-After` is not fixed by adding
   immediate retries.
3. Shed or rate-limit abusive traffic, scale API replicas within the configured
   limits, and scale run workers from queue depth. Preserve tenant fairness.
4. Roll back a regressing release. Never raise statement, task or request
   timeouts without identifying the blocked operation.
5. Resolve after p95 remains below two seconds for 30 minutes and error rate is
   normal.

