"""Apply migrations after validating the dedicated migrator configuration.

The runtime deployments never receive ``DATABASE_OWNER_URL``. This one-shot
entrypoint is the only production process that does, and it exits non-zero on
incoherent settings or a failed migration so the rollout cannot continue.
"""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config

from platform_core.observability.telemetry import (
    configure_telemetry,
    shutdown_telemetry,
    start_span,
)
from platform_core.settings import require_coherent_settings


def main() -> int:
    settings = require_coherent_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    configure_telemetry(settings)
    try:
        with start_span(
            "platform.schema.migrate",
            attributes={"platform.release": settings.release},
        ):
            command.upgrade(Config("alembic.ini"), "head")
    finally:
        shutdown_telemetry()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
