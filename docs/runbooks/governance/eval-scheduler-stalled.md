# Continuous evaluation scheduler stalled

1. Check both beat replicas, the maintenance queue and maintenance workers.
   Confirm `platform.sweep` routes only to the `maintenance` queue.
2. Verify the relay credential can execute
   `platform_schedule_due_continuous_evals` but cannot read evaluation content.
3. Restore or roll back scheduler/maintenance deployments. Duplicate sweep
   delivery is safe; do not disable a continuous-evaluation policy.
4. Confirm policy health reports no missing or overdue schedules and a new
   successful scheduler metric arrives.
5. Inspect newly created eval runs and outbox rows; avoid a manual catch-up
   storm by keeping the bounded scheduler limit.
