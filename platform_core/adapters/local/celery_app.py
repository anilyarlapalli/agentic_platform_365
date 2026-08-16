"""Celery configuration. Transport only — no correctness lives here.

The division of labour matters, because it is what makes Celery's delivery
guarantees good enough:

* **Celery delivers.** It carries a pointer saying "run X has work". It may
  deliver twice, out of order, or not at all.
* **Postgres holds.** The lease decides who may execute, the ``(run_id, step)``
  claim decides what may be applied, and the outbox decides what was ever
  intended.

So none of Celery's failure modes are correctness problems here. A duplicate
delivery loses the lease race. A lost delivery is picked up by the sweeper,
because the run is still ``pending`` in Postgres regardless of what the broker
did. This is the same reasoning the Azure build applies to Storage Queue — "job
truth lives in Postgres, the queue only carries pointers, so a lost or
duplicated message can never corrupt state" — and it is the right call.

Settings worth their explanation:

``acks_late``
    Acknowledge after the handler returns, not on receipt. With early acks a
    worker that dies mid-task loses the message entirely; the run would still be
    recovered by the reaper, but only after a lease timeout rather than
    immediately on redelivery.

``worker_prefetch_multiplier = 1``
    Do not buffer. A worker holding ten prefetched messages it has not started
    is ten runs that no other worker can take, which turns a slow task into a
    stalled queue.

``task_reject_on_worker_lost``
    Requeue rather than silently drop when the worker process vanishes.
"""

from __future__ import annotations

import atexit

from celery import Celery
from celery.signals import beat_init, worker_process_init, worker_process_shutdown

from platform_core.observability.telemetry import configure_telemetry, shutdown_telemetry
from platform_core.settings import get_settings, require_coherent_settings

TASK_RUN = "platform.execute_run"
TASK_SWEEP = "platform.sweep"


def build_celery() -> Celery:
    settings = get_settings()
    app = Celery("platform", broker=settings.celery_broker_url)

    app.conf.update(
        result_backend=settings.celery_result_backend,
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        # See the module docstring: every one of these is about not losing or
        # hoarding work.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        # Run executors and cross-tenant maintenance use separate queues and
        # credentials. A normal worker listens only to ``runs``; only the
        # maintenance deployment can consume ``platform.sweep``.
        task_default_queue="runs",
        task_routes={
            TASK_RUN: {"queue": "runs"},
            TASK_SWEEP: {"queue": "maintenance"},
        },
        # Results are diagnostic only — the run row is the record — so they can
        # expire quickly rather than accumulating in Redis.
        result_expires=3600,
        broker_connection_retry_on_startup=True,
        # A task that outruns this is not making progress; the lease will be
        # reaped anyway, so letting the worker keep the slot helps nobody.
        task_time_limit=1800,
        task_soft_time_limit=1500,
        beat_schedule={
            # The sweeper is the backstop for anything the broker lost. Without
            # it, a dropped message means a run sits `pending` until something
            # else happens to look — which is the "visible but stuck" state the
            # outbox exists to avoid.
            "sweep-pending-and-expired": {
                "task": TASK_SWEEP,
                "schedule": 30.0,
                "options": {"queue": "maintenance"},
            },
        },
    )
    return app


celery_app = build_celery()


@worker_process_init.connect
def _configure_worker_telemetry(**_kwargs) -> None:
    """Fork-safe SDK initialization inside each Celery worker process."""
    configure_telemetry(require_coherent_settings())


@worker_process_shutdown.connect
def _flush_worker_telemetry(**_kwargs) -> None:
    shutdown_telemetry()


@beat_init.connect
def _configure_scheduler_telemetry(**_kwargs) -> None:
    """Validate and instrument the singleton/HA beat processes too."""
    configure_telemetry(require_coherent_settings())
    atexit.register(shutdown_telemetry)
