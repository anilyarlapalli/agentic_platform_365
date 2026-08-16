# Release rollback

1. Stop promotion and set the candidate traffic weight to zero. Preserve its
   traces, eval run, image digest, configuration and migration job logs.
2. Roll deployments back to the last known-good image digest and release label.
   Never deploy by mutable tag.
3. Database rollback is not automatic. Migrations must use expand/migrate/
   contract so the previous application remains compatible. If compatibility
   is broken, stop and execute the reviewed forward repair; do not downgrade a
   live schema ad hoc.
4. Confirm API readiness, outbox age, run outcomes, mandatory audit writes,
   continuous-eval schedule and two-tenant authorization probes.
5. Keep the candidate blocked until the pinned evaluation gate passes on a new
   immutable release. Record cause, impact and follow-up controls.
