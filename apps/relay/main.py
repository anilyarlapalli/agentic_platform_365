"""The outbox relay: the only process that publishes to the broker.

It does one thing in a loop — move committed intent onto the wire — and it is
the only holder of the ``platform_relay`` credential, so it is the only thing in
the system that *can* read outbox rows across tenants.

## Why a separate process

It could be a thread in the API. It is not, for two reasons:

**Blast radius.** The relay credential can read every tenant's outbox. Putting
it in the API process means the API can too, and then the boundary is back to
being a convention about which function you call — which is exactly the mistake
that had to be reverted in migration 0003.

**Independent failure.** A relay that is down does not stop the API accepting
work: requests still commit run and outbox rows, and delivery resumes when the
relay returns. Coupling them would turn a relay bug into an ingress outage.

## Failure behaviour

A publish failure leaves the row unpublished, increments its attempt counter and
records the error. The row is retried on the next pass. Rows that keep failing
are reported by :func:`platform_core.correctness.outbox.poison_rows` rather than
retried silently forever — a queue that is quietly failing at a constant rate
looks identical to one that is working, from the outside.

The loop never exits on error. A relay that dies on the first bad row stops
delivering every *other* tenant's work too.
"""

from __future__ import annotations

import logging
import signal
import threading
import time

from platform_core.adapters.local.celery_queue import CeleryJobQueue
from platform_core.correctness import outbox
from platform_core.observability.telemetry import (
    configure_telemetry,
    register_outbox_observer,
    shutdown_telemetry,
)
from platform_core.ports.job_queue import QueueMessage
from platform_core.settings import require_coherent_settings

logger = logging.getLogger("platform.relay")

def _publisher(queue: CeleryJobQueue):
    def publish(row: outbox.OutboxRow) -> None:
        queue.publish(
            QueueMessage(
                id=str(row.id),
                run_id=row.run_id,
                tenant_id=row.tenant_id,
                workload=row.workload,
                payload=row.payload,
                delivery_count=0,
                enqueued_at=None,
                trace_context=row.trace_context,
            )
        )

    return publish


def run_once(queue: CeleryJobQueue | None = None) -> int:
    """One drain pass. Returns how many rows were published.

    Exposed separately from :func:`main` so tests can drive the relay
    deterministically rather than starting a daemon and waiting on a clock.
    """
    settings = require_coherent_settings()
    return outbox.drain(
        _publisher(queue or CeleryJobQueue()), batch_size=settings.relay_batch_size
    )


def main() -> int:
    settings = require_coherent_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    configure_telemetry(settings)
    register_outbox_observer(outbox.backlog)

    stopping = threading.Event()

    def _stop(*_a):
        logger.info("signal received — finishing the current batch then exiting")
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    queue = CeleryJobQueue()
    publish = _publisher(queue)
    logger.info(
        "relay started (release=%s, batch=%d)", settings.release, settings.relay_batch_size
    )

    last_report = 0.0
    while not stopping.is_set():
        try:
            published = outbox.drain(publish, batch_size=settings.relay_batch_size)
        except Exception:
            # Never exit the loop. A relay that dies on one bad batch stops
            # delivering every other tenant's work as well.
            logger.exception("drain pass failed — retrying")
            published = 0

        now = time.monotonic()
        if now - last_report >= settings.relay_backlog_report_seconds:
            last_report = now
            try:
                depth, oldest_age = outbox.backlog()
                if depth:
                    # Age, not just depth: a backlog of 3 that has not moved in
                    # an hour is a stall; 3,000 draining steadily is not.
                    logger.warning(
                        "outbox backlog: %d unpublished, oldest %.0fs", depth, oldest_age
                    )
                poison = outbox.poison_rows()
                if poison:
                    logger.error(
                        "%d outbox row(s) repeatedly failing to publish: %s",
                        len(poison), poison[:5],
                    )
            except Exception:
                logger.exception("backlog probe failed")

        if published == 0:
            stopping.wait(settings.relay_poll_interval_seconds)

    logger.info("relay stopped")
    shutdown_telemetry()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
