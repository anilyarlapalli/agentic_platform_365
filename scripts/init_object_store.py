"""Create the object store bucket. Idempotent; safe to run on every boot.

Separate from the migrations even though it is the same kind of act — "make the
substrate match what the code expects". Alembic owns Postgres and nothing else,
and giving it a step that reaches across to another service would mean a
migration can fail for reasons that have nothing to do with the schema, with no
way to roll that half back.

    .venv/bin/python -m scripts.init_object_store
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    os.environ.setdefault("SERVICE_ROLE", "test")
    os.environ.setdefault("ENVIRONMENT", "local")

    from platform_core.adapters.local.object_store import S3ObjectStore
    from platform_core.ports.errors import TransientError
    from platform_core.settings import get_settings

    settings = get_settings()
    store = S3ObjectStore()

    try:
        created = store.ensure_bucket()
    except TransientError as exc:
        print(
            f"object store unreachable at {settings.s3_endpoint_url} — is `make up` "
            f"running?\n  {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        f"bucket {settings.s3_bucket!r} "
        f"{'created' if created else 'already present'} at {settings.s3_endpoint_url}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
