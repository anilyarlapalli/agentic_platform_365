"""A terminal chat client, so the platform can be used rather than only tested.

Logs in, holds a session, and prints the sources behind every answer. The
sources are shown by default and not hidden behind a flag: an answer whose
grounding you cannot see is one you have to take on trust, which is the thing
this workload is built to avoid.

    .venv/bin/python -m scripts.chat_client [tenant] [subject]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

API = "http://127.0.0.1:8100"


def _default_subject(tenant: str) -> str:
    """Map a demo tenant slug to its seeded operator."""
    org = tenant.removeprefix("demo-").split("-")[0]
    return f"operator@{org}.example"


PASSWORD = "demo-password-1234"

DIM, BOLD, TEAL, AMBER, RED, OFF = "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[31m", "\033[0m"


def post(path: str, payload: dict, token: str | None = None) -> dict:
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise SystemExit(f"{RED}HTTP {exc.code}{OFF} {detail}") from None
    except urllib.error.URLError:
        raise SystemExit(f"{RED}No API on {API}{OFF} — start it with `make api`.") from None


def main() -> int:
    tenant = sys.argv[1] if len(sys.argv) > 1 else "acme-industrial"
    subject = sys.argv[2] if len(sys.argv) > 2 else _default_subject(tenant)

    token = post("/auth/login",
                 {"tenant": tenant, "subject": subject, "password": PASSWORD})["access_token"]
    print(f"{TEAL}{tenant}{OFF} · {subject}   {DIM}(ctrl-d to exit){OFF}\n")

    session_id: str | None = None
    spend = 0.0

    while True:
        try:
            question = input(f"{BOLD}› {OFF}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{DIM}session {session_id or '—'} · ${spend:.6f} this session{OFF}")
            return 0
        if not question:
            continue

        result = post("/api/query",
                      {"question": question, "session_id": session_id}, token)
        session_id = result["session_id"]
        spend += result["cost_usd"]

        print(f"\n{result['answer']}\n")
        if result["sources"]:
            print(f"{DIM}sources{OFF}")
            for source in result["sources"][:3]:
                print(f"  {TEAL}{source['canonical_id']}{OFF} "
                      f"{DIM}d={source['distance']:.3f}{OFF}  {source['text'][:88]}…")
        else:
            print(f"{AMBER}  not grounded — no sources matched{OFF}")

        flags = []
        if result["cache_hit"]:
            flags.append("cached")
        if not result["grounded"]:
            flags.append("ungrounded")
        print(f"{DIM}  {result['input_tokens']}→{result['output_tokens']} tok · "
              f"${result['cost_usd']:.6f} · {result['latency_ms']:.0f}ms"
              f"{' · ' + ', '.join(flags) if flags else ''}{OFF}\n")


if __name__ == "__main__":
    raise SystemExit(main())
