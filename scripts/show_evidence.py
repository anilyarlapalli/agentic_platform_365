"""Print what is currently proven, from evidence/ rather than from memory.

`make verify` tells you the suite passed. This tells you *what it established* —
each property, when it was last confirmed, and the numbers behind it.

The distinction matters after a gap: a green suite says the code is fine today,
while these records say which guarantees have actually been exercised and how
recently. A property whose record is weeks older than the rest is one whose test
has probably stopped running.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / "evidence"

GROUPS = {
    "Tenant isolation": ("tenant_", "relay_"),
    "Authorization": ("authorization_",),
    "Idempotency": ("idempotency_",),
    "Crash recovery": ("chaos_",),
    "Cost & audit": ("cost_", "budget_", "audit_"),
    "Outbox & delivery": ("outbox_",),
    "Eval gates": ("eval_",),
    "Release & scaling": ("release_", "session_", "cache_"),
    "Privilege model": ("privilege_",),
    "SQL hygiene": ("sql_",),
}


def _group_for(name: str) -> str:
    for group, prefixes in GROUPS.items():
        if name.startswith(prefixes):
            return group
    return "Other"


def main() -> int:
    props = sorted((EVIDENCE / "properties").glob("*.json"))
    if not props:
        print("No evidence recorded. Run `make verify` first.")
        return 1

    grouped: dict[str, list[dict]] = {}
    oldest: datetime | None = None
    for path in props:
        data = json.loads(path.read_text())
        grouped.setdefault(_group_for(data["property"]), []).append(data)
        stamp = datetime.fromisoformat(data["recorded_at"])
        oldest = stamp if oldest is None or stamp < oldest else oldest

    total = 0
    for group in list(GROUPS) + ["Other"]:
        entries = grouped.get(group)
        if not entries:
            continue
        print(f"\n\033[1m{group}\033[0m")
        for entry in sorted(entries, key=lambda e: e["property"]):
            mark = "\033[32m✓\033[0m" if entry.get("holds") else "\033[31m✗\033[0m"
            detail = entry.get("detail", "")
            print(f"  {mark} {entry['property']}")
            if detail:
                print(f"      {detail}")
            total += 1

    load = sorted((EVIDENCE / "load").glob("*.json"))
    if load:
        print("\n\033[1mMeasured\033[0m")
        for path in load:
            data = json.loads(path.read_text())
            numbers = {
                k: v for k, v in data.items()
                if k not in {"recorded_at", "detail"} and isinstance(v, (int, float, str))
            }
            print(f"  · {path.stem}: " + "  ".join(f"{k}={v}" for k, v in numbers.items()))

    print(f"\n{total} properties recorded across {len(grouped)} areas.")
    if oldest:
        age_days = (datetime.now(oldest.tzinfo) - oldest).days
        print(f"Oldest record: {age_days}d ago.")
        if age_days > 7:
            # A record much older than the others usually means its test stopped
            # running, not that the property stopped mattering.
            print("  \033[33mSome records are stale — re-run `make check`.\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
