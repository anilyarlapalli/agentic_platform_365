# Queue publish failures

1. Correlate failures with Redis availability, TLS/certificate changes,
   connection limits and relay release.
2. Confirm failed outbox rows remain unpublished and their attempt/error fields
   advance. That proves intent was not lost.
3. Restore the broker or roll back the relay. Do not write directly to the
   `runs` Redis list: that bypasses trace propagation and outbox accounting.
4. Watch duplicate deliveries after recovery; they are expected and safe.
5. Resolve after publish failures stop and outbox age returns below threshold.

