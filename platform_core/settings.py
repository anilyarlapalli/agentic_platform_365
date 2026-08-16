"""One typed settings object, validated at startup.

The Azure wrapper's ``config_azure`` docstring states that no other module in
its folder touches ``os.environ`` directly. ``token_budget.py`` reads four
variables at import time, so the claim is already false — and nothing detects
that, because there is no single object to be the authority.

Here, configuration is a validated Pydantic model. Nothing else in the tree
reads the environment. Two consequences worth the constraint:

* **Incoherent config fails at startup, not at first use.** The Azure build
  documents that ``EMBED_DIMENSIONS`` must match the vector dimension of the
  search index and that changing it invalidates every index — then never checks
  it. :meth:`Settings.check_coherence` does.

* **Config is inspectable.** ``/platform/health`` can report exactly what the
  process resolved, with secrets redacted, which is what turns "did the deploy
  pick up the new value" from an argument into a lookup.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent

DEV_JWT_SECRET = "dev-only-not-a-secret-padded-to-32+"
DEV_DATABASE_PASSWORD = "platform_dev_only"
DEV_S3_ACCESS_KEY = "platform"
DEV_S3_SECRET_KEY = "platform_dev_only"

# Embedding dimensions are a property of the model, not a knob. Getting this
# wrong is not a degraded result — the index rejects the write, or worse accepts
# it and returns nonsense. Pinning the map means a model change that forgets the
# dimension is a validation error rather than a silent corruption.
EMBEDDING_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "nomic-embed-text": 768,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", REPO_ROOT / ".env.local"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    # ── identity of this process ──────────────────────────────────────────
    service_role: Literal[
        "api", "worker", "relay", "maintenance", "scheduler", "migrator", "test"
    ] = "api"
    environment: Literal["local", "ci", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # The release identifier stamped on every span, metric and audit row. In
    # the Azure build this is CONTAINER_APP_REVISION; here it is the image tag
    # or git sha. Without it, "did the canary regress" has no join key.
    release: str = "dev"

    # ── postgres ──────────────────────────────────────────────────────────
    # Two DSNs on purpose. The application connects as `platform_app`, a role
    # that cannot bypass row-level security. Migrations connect as the owner.
    # Using one DSN for both would mean the runtime inherits the owner's RLS
    # bypass and every policy in every migration becomes decorative.
    database_url: PostgresDsn = Field(
        default="postgresql+psycopg://platform_app:platform_dev_only@127.0.0.1:5442/platform"
    )
    database_owner_url: PostgresDsn = Field(
        default="postgresql+psycopg://platform_owner:platform_dev_only@127.0.0.1:5442/platform"
    )
    # The relay and reaper are legitimately cross-tenant — one relay drains every
    # tenant's outbox. That privilege is a **credential**, not a runtime flag:
    # only the relay process is configured with this DSN, so no code path in the
    # API can assume it however it is called. An earlier attempt expressed the
    # same idea as a policy keyed on a session variable, and the authentication
    # path — which also opens a system session — immediately gained cross-tenant
    # read on every run. See migration 0003.
    database_relay_url: PostgresDsn = Field(
        default="postgresql+psycopg://platform_relay:platform_dev_only@127.0.0.1:5442/platform"
    )
    db_pool_size: int = 5
    db_max_overflow: int = 5
    # A statement that runs longer than this is a latency budget violation, not
    # a slow query. Killing it is how the budget stays a budget.
    db_statement_timeout_ms: int = 15_000

    # ── redis ─────────────────────────────────────────────────────────────
    redis_url: str = "redis://127.0.0.1:6389/0"
    celery_broker_url: str = "redis://127.0.0.1:6389/1"
    celery_result_backend: str = "redis://127.0.0.1:6389/2"
    relay_poll_interval_seconds: float = Field(default=1.0, ge=0.05, le=60)
    relay_batch_size: int = Field(default=100, ge=1, le=1000)
    relay_backlog_report_seconds: float = Field(default=30.0, ge=1, le=3600)
    worker_poll_interval_seconds: float = Field(default=1.0, ge=0.05, le=60)

    # ── object store ──────────────────────────────────────────────────────
    s3_endpoint_url: str = "http://127.0.0.1:9100"
    s3_access_key: SecretStr = SecretStr("platform")
    s3_secret_key: SecretStr = SecretStr("platform_dev_only")
    s3_bucket: str = "platform-artifacts"
    s3_region: str = "us-east-1"

    # ── llm ───────────────────────────────────────────────────────────────
    llm_provider: Literal["openai", "ollama", "cassette"] = "openai"
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    ollama_base_url: str = "http://127.0.0.1:11434/v1"

    llm_model_strong: str = "gpt-4o"
    llm_model_cheap: str = "gpt-4o-mini"

    # ── evaluation models ─────────────────────────────────────────────────
    # Two roles that must not collapse into the answering model.
    #
    # `llm_model_judge` grades the answer the platform produced. A judge sharing
    # a model with the answerer marks its own homework, and nothing about the
    # resulting numbers looks wrong — they are simply flattering. Enforced in
    # `check_coherence` rather than left as a comment, because it is invisible by
    # eye once it regresses.
    #
    # `llm_model_annotator` drafts the *expected* answer that the judge grades
    # against. Same argument one step earlier: if the yardstick is written by the
    # model being measured, the measurement is of self-consistency.
    # The reference deployment uses gpt-4-turbo / gpt-4o-mini for these, because
    # *its* answerer is gpt-4o. Copying those ids here would collide: this
    # platform answers with `llm_model_cheap`, which is gpt-4o-mini — so the
    # reference's annotator would be the model being measured. Both roles are
    # chosen to differ from the answerer and to exist in `MODEL_PRICING`, since
    # an unpriced model is charged at the highest known rate and every cost
    # number involving it is then wrong in a direction nobody notices.
    llm_model_judge: str = "gpt-4.1"
    llm_model_annotator: str = "gpt-4.1-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int | None = None  # derived; see check_coherence

    # Cassettes make eval gates deterministic and load tests free. `record`
    # writes real responses; `replay` refuses to reach the network at all — a
    # test that silently falls through to a live call is a test that silently
    # spends money and silently loses determinism.
    # ── retention ─────────────────────────────────────────────────────────
    # How long an unactioned corpus gap is kept. This is user content, so it
    # does not accumulate indefinitely — `apps.worker.tasks.sweep` enforces it,
    # rather than this being a documented intention like the session purge that
    # sat uncalled since Phase 5. Gaps already seeded into an eval set are
    # exempt: they have become part of a measurement.
    unanswered_question_retention_days: int = 30
    terminal_run_retention_days: int = Field(default=90, ge=7, le=3650)
    eval_history_retention_days: int = Field(default=400, ge=30, le=3650)
    usage_ledger_retention_days: int = Field(default=2555, ge=365, le=3650)
    audit_retention_days: int = Field(default=2555, ge=365, le=3650)

    # Every golden set is continuously evaluated. A policy row is created with
    # the dataset and cannot be disabled; this setting controls only cadence.
    continuous_eval_interval_seconds: int = Field(
        default=21_600, ge=900, le=604_800
    )
    continuous_eval_default_top_k: int = Field(default=5, ge=1, le=50)

    cassette_mode: Literal["off", "record", "replay"] = "off"
    cassette_dir: Path = REPO_ROOT / "evidence" / "cassettes"

    # ── cost ceilings ─────────────────────────────────────────────────────
    # Per tenant, not per process: the ceiling has to bind the thing that spends.
    daily_token_cap: int = 2_000_000
    monthly_cost_cap_usd: float = 25.0
    # The Azure build fails open and says so. That is defensible for a service
    # whose alternative is refusing to answer — but the default here is closed,
    # because an unreadable ledger means spend is unbounded AND unattributable,
    # and this platform's whole claim is that it is neither.
    budget_fail_closed: bool = True
    budget_cache_seconds: float = 5.0
    budget_reservation_ttl_seconds: int = Field(default=900, ge=30, le=3600)
    llm_default_max_output_tokens: int = Field(default=4096, ge=1, le=128_000)

    # ── telemetry ─────────────────────────────────────────────────────────
    otlp_endpoint: str = "http://127.0.0.1:4317"
    telemetry_enabled: bool = True
    telemetry_trace_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    telemetry_metric_interval_ms: int = Field(default=15_000, ge=1_000)
    # Returning None without a key rather than an unkeyed digest — an unkeyed
    # hash of an email is reversible from any user list.
    telemetry_hmac_key: SecretStr | None = None
    # Privileged actions are refused when their mandatory authorization event
    # cannot be appended. Production may never weaken this control.
    audit_fail_closed: bool = True

    # ── auth ──────────────────────────────────────────────────────────────
    # At least 32 bytes: RFC 7518 §3.2 requires a key of at least the hash
    # output size for HMAC, and PyJWT warns below it. The development default is
    # padded to a legal length rather than left short, because a warning that
    # fires on every test run is a warning nobody reads by the time it matters.
    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "local-platform"
    jwt_audience: str = "local-platform-api"
    access_token_ttl_seconds: int = 3600
    auth_cookie_name: str = "platform_session"
    browser_allowed_origins: list[str] = [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]
    # Uvicorn may trust forwarding headers only from the ingress/proxy network.
    # Trusting '*' lets any direct client forge its scheme and source address.
    trusted_proxy_ips: str = "127.0.0.1"
    verify_principal_state: bool = True

    # ── admission control ─────────────────────────────────────────────────
    # A 50 MiB binary expands to roughly 67 MiB as base64. The JSON envelope
    # gets a little headroom, but never an unbounded amount.
    max_request_body_bytes: int = Field(default=72 * 1024 * 1024, ge=1024)
    rate_limit_enabled: bool = True
    rate_limit_fail_closed: bool = True
    authenticated_requests_per_minute: int = Field(default=600, ge=1)
    login_attempts_per_window: int = Field(default=5, ge=1)
    login_window_seconds: int = Field(default=300, ge=1)

    # ── durable agent runtime ─────────────────────────────────────────────
    checkpoint_max_state_bytes: int = Field(default=1024 * 1024, ge=1024)
    tool_max_arguments_bytes: int = Field(default=64 * 1024, ge=1024)
    tool_default_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    tool_approval_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    run_retry_base_seconds: float = Field(default=2.0, ge=0.1, le=60)
    run_retry_max_seconds: float = Field(default=300.0, ge=1, le=3600)
    run_retry_jitter_ratio: float = Field(default=0.25, ge=0.0, le=1.0)

    # ── workload: graphrag ────────────────────────────────────────────────
    # Read-only import of the canonical engine tree. Nothing in this platform
    # writes to it; `check_coherence` verifies it exists before a worker that
    # needs it starts, rather than failing on the first job.
    graphrag_engine_root: Path = Path(
        "/home/anil-y/app_ideas/manufacture/R_repo/AgenticAI_Manufacturing"
    )
    graphrag_enabled: bool = False

    # ── derived ───────────────────────────────────────────────────────────

    @computed_field
    @property
    def service_name(self) -> str:
        return f"platform-{self.service_role}"

    @model_validator(mode="after")
    def _derive_embedding_dimensions(self) -> Settings:
        known = EMBEDDING_DIMENSIONS.get(self.embedding_model)
        if self.embedding_dimensions is None:
            if known is None:
                raise ValueError(
                    f"embedding_model {self.embedding_model!r} is not in the known-dimension "
                    f"map and embedding_dimensions was not set explicitly. Add it to "
                    f"EMBEDDING_DIMENSIONS rather than guessing: a wrong dimension is "
                    f"rejected at write time at best and silently meaningless at worst."
                )
            object.__setattr__(self, "embedding_dimensions", known)
        elif known is not None and known != self.embedding_dimensions:
            raise ValueError(
                f"embedding_dimensions={self.embedding_dimensions} contradicts "
                f"{self.embedding_model!r}, which is {known}-dimensional. Changing this "
                f"invalidates every existing index, so it is a migration, not a setting."
            )
        return self

    @model_validator(mode="after")
    def _validate_jwt_secret(self) -> Settings:
        """Refuse a key shorter than the HMAC output. Everywhere, not just prod.

        An HS256 key below 32 bytes reduces the effective security of every
        token the platform issues. Enforced as validation rather than as a
        production-only coherence check, because a development default that is
        illegal is a default someone eventually ships.
        """
        minimum = {"HS256": 32, "HS384": 48, "HS512": 64}[self.jwt_algorithm]
        length = len(self.jwt_secret.get_secret_value().encode("utf-8"))
        if length < minimum:
            raise ValueError(
                f"jwt_secret is {length} bytes; {self.jwt_algorithm} requires at least "
                f"{minimum} (RFC 7518 §3.2). Generate one with "
                f"`openssl rand -hex {minimum}`."
            )
        return self

    def check_coherence(self) -> list[str]:
        """Cross-field problems that should stop startup. Empty list means go.

        Separate from validation because some of these depend on the role: a
        relay does not need an LLM key, and refusing to start it for the lack of
        one would be a self-inflicted outage.
        """
        problems: list[str] = []

        # Relay moves durable pointers and must not need (or receive) an LLM
        # credential. API and worker are the only runtime roles that dispatch.
        if (
            self.service_role in {"api", "worker"}
            and self.llm_provider == "openai"
            and not self.openai_api_key
        ):
            problems.append("llm_provider=openai but openai_api_key is unset")

        if self.cassette_mode == "replay" and not self.cassette_dir.exists():
            problems.append(
                f"cassette_mode=replay but {self.cassette_dir} does not exist — "
                f"record a cassette first; replay must never fall through to the network"
            )

        if self.cassette_mode == "record" and self.llm_provider == "cassette":
            problems.append("cassette_mode=record with llm_provider=cassette records nothing")

        # The answering model is `llm_model_cheap` — `workloads/chat/service.py`
        # and `gates/runner.py` both default to it. A judge set to the same model
        # produces a number that is real, computed correctly, and biased in a
        # direction nobody can see. Checked here because the failure has no
        # symptom: the eval simply reports better pass rates for ever.
        if self.llm_model_judge == self.llm_model_cheap:
            problems.append(
                f"llm_model_judge and llm_model_cheap are both "
                f"{self.llm_model_judge!r} — the judge would be grading the model "
                f"that produced the answer. Move the judge, not the answerer."
            )
        if self.llm_model_annotator == self.llm_model_cheap:
            problems.append(
                f"llm_model_annotator and llm_model_cheap are both "
                f"{self.llm_model_annotator!r} — the model being measured would "
                f"be writing the expected answers it is measured against"
            )
        if self.llm_model_annotator == self.llm_model_judge:
            # Weaker than the rule above and still worth refusing: the annotator
            # writes the reference and the judge decides whether the answer
            # matches it, so one model doing both grades its own yardstick.
            problems.append(
                f"llm_model_annotator and llm_model_judge are both "
                f"{self.llm_model_annotator!r} — the model that wrote the "
                f"expected answer would also be deciding whether it was met"
            )

        if self.graphrag_enabled and not self.graphrag_engine_root.exists():
            problems.append(
                f"graphrag_enabled but engine root {self.graphrag_engine_root} is missing"
            )

        if self.run_retry_max_seconds < self.run_retry_base_seconds:
            problems.append(
                "run_retry_max_seconds must be greater than or equal to "
                "run_retry_base_seconds"
            )

        if self.environment in {"staging", "production"}:
            deployed = f"environment={self.environment}"
            if (
                self.service_role == "api"
                and self.jwt_secret.get_secret_value() == DEV_JWT_SECRET
            ):
                problems.append(f"{deployed} with the development jwt_secret")
            if self.service_role in {"api", "worker"} and DEV_DATABASE_PASSWORD in str(
                self.database_url
            ):
                problems.append(f"{deployed} with the development application database password")
            if self.service_role in {"relay", "maintenance"} and DEV_DATABASE_PASSWORD in str(
                self.database_relay_url
            ):
                problems.append(f"{deployed} with the development relay database password")
            if self.service_role == "migrator" and DEV_DATABASE_PASSWORD in str(
                self.database_owner_url
            ):
                problems.append(f"{deployed} with the development owner database password")
            if self.service_role in {"api", "worker"} and (
                self.s3_access_key.get_secret_value() == DEV_S3_ACCESS_KEY
                or self.s3_secret_key.get_secret_value() == DEV_S3_SECRET_KEY
            ):
                problems.append(f"{deployed} with development object-store credentials")
            if self.release == "dev":
                problems.append(f"{deployed} with release='dev'")
            if not self.telemetry_enabled:
                problems.append(f"{deployed} with telemetry_enabled=false")
            if self.service_role == "api" and not self.audit_fail_closed:
                problems.append(f"{deployed} with audit_fail_closed=false")
            if not self.otlp_endpoint.startswith("https://"):
                problems.append(f"{deployed} with a non-TLS otlp_endpoint")
            if not self.telemetry_hmac_key:
                problems.append(
                    f"{deployed} without telemetry_hmac_key — user "
                    "pseudonymisation would be disabled"
                )
            if self.service_role in {"api", "worker"} and not self.budget_fail_closed:
                problems.append(f"{deployed} with budget_fail_closed=false")
            if self.service_role == "api" and not self.rate_limit_enabled:
                problems.append(f"{deployed} with rate_limit_enabled=false")
            if self.service_role == "api" and not self.rate_limit_fail_closed:
                problems.append(f"{deployed} with rate_limit_fail_closed=false")
            if self.service_role == "api":
                if not self.trusted_proxy_ips.strip() or self.trusted_proxy_ips.strip() == "*":
                    problems.append(
                        f"{deployed} with trusted_proxy_ips={self.trusted_proxy_ips!r}"
                    )
                insecure_origins = [
                    origin
                    for origin in self.browser_allowed_origins
                    if not origin.startswith("https://")
                ]
                if insecure_origins:
                    problems.append(
                        f"{deployed} with non-HTTPS browser_allowed_origins: "
                        + ", ".join(insecure_origins)
                    )

        return problems

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def require_coherent_settings() -> Settings:
    """Load settings and exit loudly if they cannot work. Call once, at startup.

    Exiting beats degrading: a process that starts with an unusable
    configuration will fail later, further from the cause, usually while holding
    a lease on somebody's job.
    """
    settings = get_settings()
    problems = settings.check_coherence()
    if problems:
        print(
            f"[{settings.service_name}] refusing to start — incoherent configuration:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  · {problem}", file=sys.stderr)
        raise SystemExit(78)  # EX_CONFIG
    return settings
