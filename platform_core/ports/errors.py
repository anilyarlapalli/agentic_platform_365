"""Port-level errors, so callers do not handle every adapter's exception tree.

The distinction that matters operationally is :class:`TransientError` versus
everything else, because it is the one the retry and requeue logic branches on.

The Azure worker gets this wrong in an instructive way. ``process_job`` catches
every exception, records ``status='failed'``, and deliberately does not re-raise
— its comment says transient faults are retried by the caller's exception path.
But that path only runs if ``process_job`` raises, which it now cannot. So a
Storage blip and a malformed PDF are recorded identically as permanent
failures, and the message is deleted either way.

Making transience a *type* rather than a comment is what lets the queue layer
decide correctly without knowing what the workload was doing.
"""

from __future__ import annotations


class PortError(Exception):
    """Base for every error a port is allowed to raise."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause


class TransientError(PortError):
    """The operation may succeed if retried: timeout, throttle, 5xx, broken socket.

    Retryable by definition. An adapter that cannot tell must not guess this —
    classifying a deterministic failure as transient turns one bad message into
    an infinite redelivery loop, which is a worse outage than the original
    failure.
    """

    def __init__(
        self, message: str, *, cause: BaseException | None = None, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message, cause=cause)
        # Honour the server's own backoff when it supplies one. Guessing against
        # a service that has told you exactly how long to wait is how a throttle
        # becomes a thundering herd.
        self.retry_after_s = retry_after_s


class NotFoundError(PortError):
    """The addressed object does not exist, or is not visible to this tenant.

    Deliberately one error rather than two. Distinguishing "absent" from
    "belongs to someone else" is an existence oracle: it tells a caller that a
    key exists in another tenant, which is a real leak even when the content
    never crosses.
    """


class ConflictError(PortError):
    """The operation lost a race, or would violate a uniqueness guarantee.

    Usually not an error at all at the call site: it is how idempotency
    announces that the work is already done or already claimed.
    """


class BudgetExceededError(PortError):
    """A ceiling was reached, and the call was not dispatched.

    Raised *before* spending, never after. An error reported after the tokens
    are gone is a receipt, not a control.
    """
