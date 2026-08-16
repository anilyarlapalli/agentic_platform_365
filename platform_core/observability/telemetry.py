"""OpenTelemetry bootstrap and the platform's low-cardinality signal contract.

No prompt, completion, document text, email address, tenant slug, tool argument,
or object key is emitted here. Traces carry keyed pseudonyms for correlation;
metrics deliberately carry no tenant or principal dimension.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import socket
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from opentelemetry import metrics, trace
from opentelemetry.metrics import Observation
from opentelemetry.trace import SpanKind, Status, StatusCode

from platform_core.identity.principal import RequestContext
from platform_core.settings import Settings, get_settings

logger = logging.getLogger("platform.telemetry")

_lock = threading.Lock()
_configured = False
_configuration_error: str | None = None
_providers: list[Any] = []
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")

_meter = metrics.get_meter("platform.runtime")
HTTP_REQUESTS = _meter.create_counter(
    "platform.http.server.requests", unit="{request}", description="Completed HTTP requests"
)
HTTP_DURATION = _meter.create_histogram(
    "platform.http.server.duration", unit="ms", description="HTTP request duration"
)
LLM_CALLS = _meter.create_counter(
    "platform.gen_ai.calls", unit="{call}", description="Logical model calls"
)
LLM_DURATION = _meter.create_histogram(
    "platform.gen_ai.duration", unit="ms", description="Logical model call duration"
)
LLM_TOKENS = _meter.create_counter(
    "platform.gen_ai.tokens", unit="{token}", description="Provider-reported model tokens"
)
LLM_COST = _meter.create_counter(
    "platform.gen_ai.cost", unit="USD", description="Attributed model cost"
)
QUEUE_PUBLISH = _meter.create_counter(
    "platform.queue.publish", unit="{message}", description="Queue publish outcomes"
)
RUN_OUTCOMES = _meter.create_counter(
    "platform.run.outcomes", unit="{run}", description="Durable run outcomes"
)
RUN_RECOVERIES = _meter.create_counter(
    "platform.run.recoveries",
    unit="{run}",
    description="Expired run leases recovered by the maintenance reaper",
)
AUDIT_WRITES = _meter.create_counter(
    "platform.audit.writes", unit="{event}", description="Audit append outcomes"
)
EVAL_GATES = _meter.create_counter(
    "platform.eval.gates", unit="{evaluation}", description="Continuous evaluation verdicts"
)
EVAL_SCHEDULER = _meter.create_counter(
    "platform.eval.scheduler.passes",
    unit="{pass}",
    description="Continuous evaluation scheduler outcomes",
)
RETENTION_ROWS = _meter.create_counter(
    "platform.retention.rows", unit="{row}", description="Rows removed by lifecycle policy"
)
RETENTION_PASSES = _meter.create_counter(
    "platform.retention.passes",
    unit="{pass}",
    description="Retention enforcement outcomes",
)
AUTH_ATTEMPTS = _meter.create_counter(
    "platform.auth.attempts", unit="{attempt}", description="Authentication outcomes"
)
ADMISSION_DECISIONS = _meter.create_counter(
    "platform.admission.decisions",
    unit="{decision}",
    description="Rate-limit admission outcomes",
)
BUDGET_DECISIONS = _meter.create_counter(
    "platform.budget.decisions",
    unit="{decision}",
    description="Pre-dispatch budget control outcomes",
)
BUDGET_LEDGER_WRITES = _meter.create_counter(
    "platform.budget.ledger.writes",
    unit="{write}",
    description="Budget reservation and usage-ledger write outcomes",
)
TOOL_EXECUTIONS = _meter.create_counter(
    "platform.tool.executions",
    unit="{execution}",
    description="Governed tool execution outcomes",
)
TOOL_DURATION = _meter.create_histogram(
    "platform.tool.duration",
    unit="ms",
    description="Governed tool execution duration",
)
_outbox_instruments: list[Any] = []


class TelemetryConfigurationError(RuntimeError):
    """Mandatory telemetry could not be configured safely."""


class _PrivacyLoggingHandler:
    """Factory namespace kept private to avoid importing SDK log APIs eagerly."""

    @staticmethod
    def build(provider, settings: Settings):
        from opentelemetry._logs import SeverityNumber

        severity = {
            logging.DEBUG: SeverityNumber.DEBUG,
            logging.INFO: SeverityNumber.INFO,
            logging.WARNING: SeverityNumber.WARN,
            logging.ERROR: SeverityNumber.ERROR,
            logging.CRITICAL: SeverityNumber.FATAL,
        }

        class PrivacyHandler(logging.Handler):
            """Bridge stdlib logs to the stable OTel Logger API.

            The SDK's ``LoggingHandler`` is deprecated as of 1.44. Building the
            small bridge here also gives us a strict attribute allowlist and
            lets exception text pass through the same privacy scrubber as the
            message body.
            """

            def __init__(self) -> None:
                super().__init__(level=logging.INFO)
                self._platform_telemetry_handler = True

            def emit(self, record: logging.LogRecord) -> None:
                # Exporter diagnostics routed back through the exporter recurse
                # until the process runs out of stack. They remain on the local
                # handlers and are covered by collector health/metrics instead.
                if record.name.startswith("opentelemetry."):
                    return
                try:
                    attributes: dict[str, Any] = {
                        "code.file.path": record.pathname,
                        "code.function.name": record.funcName,
                        "code.line.number": record.lineno,
                        "thread.name": record.threadName,
                    }
                    if record.exc_info:
                        exception = logging.Formatter().formatException(record.exc_info)
                        attributes["exception.type"] = record.exc_info[0].__name__
                        attributes["exception.stacktrace"] = _sanitize(exception, settings)
                    provider.get_logger(record.name).emit(
                        timestamp=int(record.created * 1_000_000_000),
                        severity_number=severity.get(record.levelno, SeverityNumber.UNSPECIFIED),
                        severity_text=record.levelname,
                        body=_sanitize(record.getMessage(), settings),
                        attributes=attributes,
                    )
                except Exception:
                    self.handleError(record)

        return PrivacyHandler()


def configure_telemetry(settings: Settings | None = None) -> None:
    """Install trace, metric and log exporters exactly once per process."""
    global _configured, _configuration_error
    settings = settings or get_settings()
    if not settings.telemetry_enabled:
        return

    with _lock:
        if _configured:
            return
        try:
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

            insecure = settings.otlp_endpoint.startswith("http://")
            resource = Resource.create(
                {
                    "service.name": settings.service_name,
                    "service.version": settings.release,
                    "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
                    "deployment.environment.name": settings.environment,
                }
            )

            tracer_provider = TracerProvider(
                resource=resource,
                sampler=ParentBased(TraceIdRatioBased(settings.telemetry_trace_sample_ratio)),
            )
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=insecure)
                )
            )
            trace.set_tracer_provider(tracer_provider)

            metric_reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=settings.otlp_endpoint, insecure=insecure),
                export_interval_millis=settings.telemetry_metric_interval_ms,
            )
            meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
            metrics.set_meter_provider(meter_provider)

            logger_provider = LoggerProvider(resource=resource)
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    OTLPLogExporter(endpoint=settings.otlp_endpoint, insecure=insecure)
                )
            )
            from opentelemetry._logs import set_logger_provider

            set_logger_provider(logger_provider)
            root_logger = logging.getLogger()
            if not any(
                getattr(handler, "_platform_telemetry_handler", False)
                for handler in root_logger.handlers
            ):
                root_logger.addHandler(_PrivacyLoggingHandler.build(logger_provider, settings))

            # Instrument engine construction before the first pool is created.
            SQLAlchemyInstrumentor().instrument(
                tracer_provider=tracer_provider,
                meter_provider=meter_provider,
                enable_commenter=False,
            )

            _providers.extend([tracer_provider, meter_provider, logger_provider])
            _configured = True
            _configuration_error = None
        except Exception as exc:
            _configuration_error = f"{type(exc).__name__}: {exc}"
            if settings.environment in {"staging", "production"}:
                raise TelemetryConfigurationError(_configuration_error) from exc
            logger.exception("telemetry configuration failed")


def instrument_fastapi(app) -> None:
    """Install ASGI tracing before the application's middleware stack is built."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=r".*/health$",
        )
    except Exception as exc:
        settings = get_settings()
        if settings.environment in {"staging", "production"}:
            raise TelemetryConfigurationError(
                f"FastAPI instrumentation failed: {type(exc).__name__}: {exc}"
            ) from exc
        logger.exception("FastAPI instrumentation failed")


def telemetry_status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "enabled": settings.telemetry_enabled,
        "configured": _configured,
        "endpoint": settings.otlp_endpoint,
        "error": _configuration_error,
    }


def collector_reachable(timeout_seconds: float = 0.5) -> tuple[bool, str | None]:
    """Check the configured OTLP socket without emitting user data."""
    endpoint = urlparse(get_settings().otlp_endpoint)
    host = endpoint.hostname
    port = endpoint.port or (443 if endpoint.scheme == "https" else 4317)
    if not host:
        return False, "invalid_endpoint"
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True, None
    except OSError as exc:
        return False, type(exc).__name__


def shutdown_telemetry(timeout_millis: int = 5_000) -> None:
    """Flush boundedly during graceful shutdown."""
    for provider in tuple(_providers):
        try:
            force_flush = getattr(provider, "force_flush", None)
            if force_flush:
                force_flush(timeout_millis=timeout_millis)
            shutdown = getattr(provider, "shutdown", None)
            if shutdown:
                shutdown()
        except Exception:
            logger.exception("telemetry provider did not shut down cleanly")


def pseudonym(value: object, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    key = settings.telemetry_hmac_key or settings.jwt_secret
    digest = hmac.new(
        key.get_secret_value().encode("utf-8"),
        str(value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:24]


def bind_request_context(ctx: RequestContext) -> None:
    """Attach privacy-safe identity and causal identifiers to the current span."""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.set_attributes(
        {
            "platform.tenant.id": pseudonym(ctx.tenant.id),
            "enduser.id": pseudonym(ctx.principal.id),
            "platform.request.id": str(ctx.request_id),
            "platform.correlation.id": str(ctx.correlation_id or ctx.request_id),
            "platform.run.id": str(ctx.run_id) if ctx.run_id else "",
        }
    )


def record_http(method: str, route: str, status_code: int, duration_ms: float) -> None:
    attributes = {
        "http.request.method": method,
        "http.route": route,
        "http.response.status_code": status_code,
    }
    HTTP_REQUESTS.add(1, attributes)
    HTTP_DURATION.record(duration_ms, attributes)


def record_llm(
    *,
    provider: str,
    operation: str,
    model: str,
    outcome: str,
    duration_ms: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
) -> None:
    attributes = {
        "gen_ai.provider.name": provider,
        "gen_ai.operation.name": operation,
        "gen_ai.request.model": model,
        "platform.outcome": outcome,
    }
    LLM_CALLS.add(1, attributes)
    LLM_DURATION.record(duration_ms, attributes)
    if input_tokens:
        LLM_TOKENS.add(input_tokens, {**attributes, "gen_ai.token.type": "input"})
    if output_tokens:
        LLM_TOKENS.add(output_tokens, {**attributes, "gen_ai.token.type": "output"})
    if cost_usd:
        LLM_COST.add(cost_usd, attributes)


def record_queue_publish(transport: str, outcome: str) -> None:
    QUEUE_PUBLISH.add(1, {"messaging.system": transport, "platform.outcome": outcome})


def record_run_outcome(workload: str, outcome: str) -> None:
    RUN_OUTCOMES.add(1, {"platform.workload": workload, "platform.outcome": outcome})


def record_run_recovery(outcome: str, count: int = 1) -> None:
    if count:
        RUN_RECOVERIES.add(count, {"platform.outcome": outcome})


def record_audit_write(outcome: str, *, required: bool) -> None:
    AUDIT_WRITES.add(
        1,
        {
            "platform.outcome": outcome,
            "platform.audit.required": str(required).lower(),
        },
    )


def record_eval_gate(outcome: str) -> None:
    """Record only the verdict; dataset names are intentionally not metric labels."""
    EVAL_GATES.add(1, {"platform.outcome": outcome})


def record_eval_scheduler(outcome: str, *, scheduled: int = 0) -> None:
    EVAL_SCHEDULER.add(
        1,
        {"platform.outcome": outcome, "platform.eval.scheduled": str(scheduled > 0).lower()},
    )


def record_retention_rows(category: str, count: int) -> None:
    if count:
        RETENTION_ROWS.add(count, {"platform.retention.category": category})


def record_retention_pass(outcome: str) -> None:
    RETENTION_PASSES.add(1, {"platform.outcome": outcome})


def record_auth_attempt(outcome: str) -> None:
    AUTH_ATTEMPTS.add(1, {"platform.outcome": outcome})


def record_admission_decision(scope: str, outcome: str) -> None:
    ADMISSION_DECISIONS.add(
        1,
        {"platform.admission.scope": scope, "platform.outcome": outcome},
    )


def record_budget_decision(stage: str, outcome: str) -> None:
    BUDGET_DECISIONS.add(
        1,
        {"platform.budget.stage": stage, "platform.outcome": outcome},
    )


def record_budget_ledger_write(operation: str, outcome: str) -> None:
    BUDGET_LEDGER_WRITES.add(
        1,
        {"platform.budget.operation": operation, "platform.outcome": outcome},
    )


def record_tool_execution(
    tool_name: str,
    side_effect: str,
    outcome: str,
    *,
    duration_ms: float | None = None,
) -> None:
    # Tool names come only from the bounded registry, never from user input.
    attributes = {
        "gen_ai.tool.name": tool_name,
        "platform.tool.side_effect": side_effect,
        "platform.outcome": outcome,
    }
    TOOL_EXECUTIONS.add(1, attributes)
    if duration_ms is not None:
        TOOL_DURATION.record(duration_ms, attributes)


@contextmanager
def start_span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
):
    tracer = trace.get_tracer("platform.runtime")
    with tracer.start_as_current_span(name, kind=kind, attributes=dict(attributes or {})) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            span.record_exception(exc)
            raise


def register_outbox_observer(callback) -> None:
    """Register a non-tenant-labelled outbox gauge callback."""
    if _outbox_instruments:
        return

    def observe_depth(_options):
        try:
            depth, _oldest = callback()
            yield Observation(depth)
        except Exception:
            return

    def observe_age(_options):
        try:
            _depth, oldest = callback()
            yield Observation(oldest)
        except Exception:
            return

    _outbox_instruments.extend(
        [
            _meter.create_observable_gauge(
                "platform.outbox.pending",
                callbacks=[observe_depth],
                unit="{row}",
                description="Unpublished transactional outbox rows",
            ),
            _meter.create_observable_gauge(
                "platform.outbox.oldest_age",
                callbacks=[observe_age],
                unit="s",
                description="Age of the oldest unpublished outbox row",
            ),
        ]
    )


def _sanitize(value: str, settings: Settings) -> str:
    return _EMAIL.sub(lambda match: f"user:{pseudonym(match.group(0), settings)}", value)
