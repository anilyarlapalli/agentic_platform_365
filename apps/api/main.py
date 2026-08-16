"""Run the API with an explicit, validated proxy trust boundary."""

from __future__ import annotations

import uvicorn

from platform_core.settings import require_coherent_settings


def main() -> int:
    settings = require_coherent_settings()
    uvicorn.run(
        "platform_core.api.app:app",
        host="0.0.0.0",  # noqa: S104 - container listener; Service controls exposure
        port=8100,
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxy_ips,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
