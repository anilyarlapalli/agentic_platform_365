"""Static release-policy checks that need no cluster credentials.

This is intentionally stricter than a YAML parser. It verifies the production
properties the manifests are meant to carry: process credential separation,
restricted pods, resources, redundancy, disruption budgets, fail-closed
networking, queue-driven worker scaling, runbook coverage, and secret hygiene.
Use ``--release`` on fully rendered manifests to additionally require immutable
image digests and reject every ``unreleased`` placeholder.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "deploy" / "k8s" / "base"
DIGEST_IMAGE = re.compile(r"^[^\s]+@sha256:[0-9a-f]{64}$")
DIRECT_SCRIPT_LAUNCH = re.compile(
    r"(?:(?:\.venv/bin/)?python3?|\$\(PY\))\s+scripts/([A-Za-z0-9_/-]+)\.py"
)
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OpenAI-style key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}
SCAN_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".json", ".yaml", ".yml", ".md", ".toml"}
SCAN_EXCLUDED = {".git", ".venv", ".next", "node_modules", "evidence", "backups"}
FORBIDDEN_ENV_BY_ROLE = {
    "api": {
        "DATABASE_OWNER_URL", "DATABASE_RELAY_URL", "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
    },
    "worker": {"DATABASE_OWNER_URL", "DATABASE_RELAY_URL", "JWT_SECRET"},
    "relay": {
        "DATABASE_URL", "DATABASE_OWNER_URL", "OPENAI_API_KEY", "JWT_SECRET",
        "S3_ACCESS_KEY", "S3_SECRET_KEY", "REDIS_URL",
    },
    "maintenance": {
        "DATABASE_URL", "DATABASE_OWNER_URL", "OPENAI_API_KEY", "JWT_SECRET",
        "S3_ACCESS_KEY", "S3_SECRET_KEY", "REDIS_URL",
    },
    "scheduler": {
        "DATABASE_URL", "DATABASE_OWNER_URL", "DATABASE_RELAY_URL", "OPENAI_API_KEY",
        "JWT_SECRET", "S3_ACCESS_KEY", "S3_SECRET_KEY", "REDIS_URL",
    },
    "migrator": {
        "DATABASE_URL", "DATABASE_RELAY_URL", "OPENAI_API_KEY", "JWT_SECRET",
        "S3_ACCESS_KEY", "S3_SECRET_KEY", "REDIS_URL", "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
    },
}


def _documents(base: Path = BASE, rendered: Path | None = None) -> list[dict[str, Any]]:
    if rendered is not None:
        return [doc for doc in yaml.safe_load_all(rendered.read_text()) if doc]
    manifest = yaml.safe_load((base / "kustomization.yaml").read_text())
    docs: list[dict[str, Any]] = []
    for relative in manifest.get("resources", []):
        path = base / relative
        if not path.is_file():
            raise ValueError(f"kustomization resource does not exist: {relative}")
        docs.extend(doc for doc in yaml.safe_load_all(path.read_text()) if doc)
    return docs


def _pod_template(doc: dict[str, Any]) -> dict[str, Any] | None:
    kind = doc.get("kind")
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        return doc["spec"]["template"]
    if kind == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]
    return None


def _env_names(container: dict[str, Any]) -> set[str]:
    return {item.get("name", "") for item in container.get("env", [])}


def _check_manifests(docs: list[dict[str, Any]], *, release: bool) -> list[str]:
    errors: list[str] = []
    deployments: set[str] = set()
    pdbs: set[str] = set()
    config: dict[str, str] = {}
    default_deny = False
    worker_scaler = False

    for doc in docs:
        kind = doc.get("kind", "unknown")
        name = doc.get("metadata", {}).get("name", "unnamed")
        where = f"{kind}/{name}"
        if kind == "ConfigMap" and name == "platform-runtime-config":
            config = doc.get("data", {})
        if kind == "Deployment":
            deployments.add(name)
            if int(doc.get("spec", {}).get("replicas", 0)) < 2:
                errors.append(f"{where}: production deployment needs at least two replicas")
        if kind == "PodDisruptionBudget":
            pdbs.add(name)
        if kind == "NetworkPolicy" and name == "default-deny":
            policy_types = set(doc.get("spec", {}).get("policyTypes", []))
            default_deny = {"Ingress", "Egress"}.issubset(policy_types)
        if kind == "ScaledObject" and name == "platform-worker":
            spec = doc.get("spec", {})
            fallback = spec.get("fallback", {})
            worker_scaler = bool(
                spec.get("minReplicaCount", 0) >= 2
                and spec.get("maxReplicaCount", 0) >= 10
                and fallback.get("failureThreshold")
                and fallback.get("replicas", 0) >= 2
            )

        template = _pod_template(doc)
        if template is None:
            continue
        pod_spec = template.get("spec", {})
        pod_security = pod_spec.get("securityContext", {})
        if pod_spec.get("automountServiceAccountToken") is not False:
            errors.append(f"{where}: service-account token automount must be false")
        if not pod_spec.get("serviceAccountName"):
            errors.append(f"{where}: a dedicated service account is required")
        if pod_security.get("runAsNonRoot") is not True:
            errors.append(f"{where}: runAsNonRoot must be true")
        if pod_security.get("seccompProfile", {}).get("type") != "RuntimeDefault":
            errors.append(f"{where}: RuntimeDefault seccomp is required")

        for container in pod_spec.get("containers", []):
            container_where = f"{where}/{container.get('name', 'unnamed')}"
            image = str(container.get("image", ""))
            if not image or image.endswith(":latest"):
                errors.append(f"{container_where}: image is missing or mutable latest")
            if release and not DIGEST_IMAGE.fullmatch(image):
                errors.append(f"{container_where}: rendered release image is not digest-pinned")
            resources = container.get("resources", {})
            if not resources.get("requests") or not resources.get("limits"):
                errors.append(f"{container_where}: resource requests and limits are required")
            security = container.get("securityContext", {})
            if security.get("allowPrivilegeEscalation") is not False:
                errors.append(f"{container_where}: privilege escalation must be disabled")
            if security.get("readOnlyRootFilesystem") is not True:
                errors.append(f"{container_where}: root filesystem must be read-only")
            if "ALL" not in security.get("capabilities", {}).get("drop", []):
                errors.append(f"{container_where}: all Linux capabilities must be dropped")

            names = _env_names(container)
            role_item = next(
                (item for item in container.get("env", []) if item.get("name") == "SERVICE_ROLE"),
                {},
            )
            role = role_item.get("value")
            forbidden = names & FORBIDDEN_ENV_BY_ROLE.get(role, set())
            if forbidden:
                errors.append(
                    f"{container_where}: role {role} receives forbidden secrets {sorted(forbidden)}"
                )

        if release:
            labels = template.get("metadata", {}).get("labels", {})
            if labels.get("app.kubernetes.io/version") in {None, "unreleased"}:
                errors.append(f"{where}: rendered release label is still unreleased")
            if "unreleased" in name:
                errors.append(f"{where}: rendered release resource name is still unreleased")

    required_true = {
        "TELEMETRY_ENABLED", "AUDIT_FAIL_CLOSED", "BUDGET_FAIL_CLOSED",
        "RATE_LIMIT_ENABLED", "RATE_LIMIT_FAIL_CLOSED", "VERIFY_PRINCIPAL_STATE",
    }
    if config.get("ENVIRONMENT") != "production":
        errors.append("ConfigMap/platform-runtime-config: ENVIRONMENT must be production")
    for key in required_true:
        if config.get(key) != "true":
            errors.append(f"ConfigMap/platform-runtime-config: {key} must be true")
    if not config.get("OTLP_ENDPOINT", "").startswith("https://"):
        errors.append("ConfigMap/platform-runtime-config: OTLP endpoint must use TLS")
    if set(deployments) - pdbs:
        errors.append(f"deployments missing disruption budgets: {sorted(set(deployments) - pdbs)}")
    if not default_deny:
        errors.append("NetworkPolicy/default-deny must deny ingress and egress")
    if not worker_scaler:
        errors.append("ScaledObject/platform-worker must have bounded scaling and fallback")
    return errors


def _check_compose_role_separation() -> list[str]:
    path = ROOT / "deploy" / "compose.runtime.yml"
    try:
        compose = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        return [f"could not load {path.relative_to(ROOT)}: {exc}"]

    errors: list[str] = []
    for service_name, service in compose.get("services", {}).items():
        environment = service.get("environment", {})
        if not isinstance(environment, dict):
            errors.append(f"compose service {service_name}: environment must be a mapping")
            continue
        role = environment.get("SERVICE_ROLE")
        if role is None and service_name != "web":
            errors.append(f"compose service {service_name}: SERVICE_ROLE is missing")
            continue
        forbidden = set(environment) & FORBIDDEN_ENV_BY_ROLE.get(role, set())
        if forbidden:
            errors.append(
                f"compose service {service_name}: role {role} receives forbidden "
                f"credentials {sorted(forbidden)}"
            )
    return errors


def _check_runbooks() -> list[str]:
    rules = yaml.safe_load((ROOT / "deploy" / "prometheus" / "rules" / "platform.yml").read_text())
    errors: list[str] = []
    for group in rules.get("groups", []):
        for rule in group.get("rules", []):
            runbook = rule.get("annotations", {}).get("runbook")
            alert = rule.get("alert", "unnamed")
            if not runbook:
                errors.append(f"alert {alert} has no runbook annotation")
                continue
            path = ROOT / "docs" / "runbooks" / f"{runbook}.md"
            if not path.is_file():
                errors.append(f"alert {alert} references missing runbook {path.relative_to(ROOT)}")
    return errors


def _check_secrets() -> list[str]:
    errors: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        if any(part in SCAN_EXCLUDED for part in path.parts):
            continue
        if path.name.startswith(".env") and path.name != ".env.example":
            continue
        try:
            value = path.read_text(errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(value):
                errors.append(f"{path.relative_to(ROOT)} contains a probable {label}")
    return errors


def _check_python_entrypoints() -> list[str]:
    """Keep repository commands independent of Python's script-path semantics."""
    paths = [
        ROOT / "Makefile",
        ROOT / ".github" / "workflows" / "ci.yml",
        ROOT / ".github" / "workflows" / "release.yml",
        ROOT / "README.md",
        ROOT / "docs" / "production-readiness.md",
        *(ROOT / "scripts").glob("*.py"),
    ]
    errors: list[str] = []
    for path in paths:
        try:
            value = path.read_text()
        except OSError as exc:
            errors.append(f"could not inspect Python entrypoints in {path.relative_to(ROOT)}: {exc}")
            continue
        for match in DIRECT_SCRIPT_LAUNCH.finditer(value):
            module = match.group(1).replace("/", ".")
            errors.append(
                f"{path.relative_to(ROOT)} launches a Python file directly; "
                f"use `python -m scripts.{module}`"
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--release",
        action="store_true",
        help="also require rendered release labels, names and digest-pinned images",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--manifest-dir", type=Path, default=BASE)
    source.add_argument("--rendered", type=Path)
    args = parser.parse_args()
    try:
        docs = _documents(args.manifest_dir, args.rendered)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"deployment policy: could not load manifests: {exc}", file=sys.stderr)
        return 1
    errors = _check_manifests(docs, release=args.release)
    errors.extend(_check_compose_role_separation())
    errors.extend(_check_runbooks())
    errors.extend(_check_secrets())
    errors.extend(_check_python_entrypoints())
    if errors:
        print("deployment policy failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"deployment policy passed ({len(docs)} resources)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
