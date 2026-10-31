"""Cron fast-fail for a rate limit whose cooldown is already known (t-b5428f74).

A cron job pinned to one provider/model has no fallback chain (``cron.scheduler._job_fallback_chain``),
so a 429 that carries ``Retry-After: 600`` used to park the run in 600s sleeps for up to
``agent.api_max_retries`` rounds, holding the job's "already running" claim and skipping every
later shift. When the provider has said the window stays closed for at least
``agent.cron_rate_limit_fail_fast_seconds`` and nothing is left to fall back to, the run ends now
and the next scheduled occurrence probes again. Cron-only; 0 (default) keeps the old behaviour.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from agent.error_classifier import FailoverReason

_FAIL_FAST_REASONS = frozenset({FailoverReason.rate_limit, FailoverReason.upstream_rate_limit})


def known_cooldown_seconds(api_error: Any, error_context: Any) -> Optional[float]:
    """Seconds the provider said the limit lasts (``Retry-After`` first, else ``reset_at``), uncapped."""
    from agent.credential_pool import _parse_absolute_timestamp
    from agent.turn_recovery_autorecover import _retry_after_seconds

    retry_after = _retry_after_seconds(api_error)
    if retry_after is not None:
        return retry_after
    reset_at = error_context.get("reset_at") if isinstance(error_context, dict) else None
    remaining = (_parse_absolute_timestamp(reset_at) or 0.0) - time.time()
    return remaining if remaining > 0 else None


def should_fail_fast(agent: Any, classified: Any, api_error: Any, error_context: Any) -> bool:
    """True when this cron turn should end on a rate limit instead of sleeping through it."""
    threshold = float(getattr(agent, "_cron_rate_limit_fail_fast_s", 0) or 0)
    if threshold <= 0 or str(getattr(agent, "platform", "") or "").lower() != "cron":
        return False
    if classified.reason not in _FAIL_FAST_REASONS:
        return False
    cooldown = known_cooldown_seconds(api_error, error_context)
    return cooldown is not None and cooldown >= threshold
