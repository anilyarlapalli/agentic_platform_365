#!/usr/bin/env bash
# Audit the locked dependency set for known vulnerabilities.
#
# Deliberately audits the *lock*, not the installed environment.
#
# `pip-audit --strict` over an environment audits whatever happens to be
# installed there — which, since the packaging fix, includes this project
# itself. `local-platform` is not on PyPI, so the lookup fails, and `--strict`
# turns "could not be audited" into a build failure. That is a false negative
# dressed as a vulnerability: it reports a problem with a package that has no
# published advisories to find. `--skip-editable` does not help, because
# `--strict` treats a skipped distribution as an error too.
#
# Exporting the lock with `--no-emit-project` audits exactly the third-party
# dependencies that ship, and nothing else. It also makes the check independent
# of how the developer's virtual environment was built, which is the same
# reason the lock is the authority everywhere else in this repository.
#
# Override UV when the tool is not on PATH (CI bootstraps a pinned copy):
#   UV=/tmp/platform-uv-bootstrap/bin/uv scripts/audit_python_dependencies.sh
set -euo pipefail

UV="${UV:-uv}"
PIP_AUDIT="${PIP_AUDIT:-.venv/bin/pip-audit}"

requirements="$(mktemp -t platform-requirements-XXXXXX.txt)"
trap 'rm -f "$requirements"' EXIT

"$UV" export --frozen --extra dev --no-emit-project --quiet -o "$requirements"

count="$(grep -c '==' "$requirements" || true)"
echo "auditing ${count} locked dependencies (project itself excluded)"

# --no-deps because every requirement is already pinned by the lock; resolving
# again would audit a set the build would never install.
"$PIP_AUDIT" --strict --no-deps -r "$requirements"
