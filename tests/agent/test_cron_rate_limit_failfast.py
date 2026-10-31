"""A cron run ends on a rate limit with a known long cooldown instead of sleeping through it.

Behaviour contract: a pinned cron job (no fallback chain) that gets a 429 whose ``Retry-After`` is at
least ``agent.cron_rate_limit_fail_fast_seconds`` returns the terminal result at once, so the job's
"already running" claim is released for the next occurrence. Chat surfaces, short cooldowns and a
disabled setting keep the ordinary backoff-and-retry.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import turn_api_error
from agent.error_classifier import FailoverReason
from agent.turn_rate_limit_failfast import known_cooldown_seconds, should_fail_fast


def _429(retry_after=None):
    err = Exception("HTTP 429: This request would exceed your account's rate limit")
    err.response = SimpleNamespace(headers={"retry-after": str(retry_after)} if retry_after is not None else {})
    return err


def _agent(platform="cron", threshold=120):
    agent = MagicMock()
    agent.platform = platform
    agent._cron_rate_limit_fail_fast_s = threshold
    return agent


def _classified(reason=FailoverReason.rate_limit):
    return SimpleNamespace(reason=reason, retryable=True, should_compress=False, should_fallback=True, is_auth=False)


def test_known_cooldown_reads_retry_after_then_reset_at():
    assert known_cooldown_seconds(_429(600), None) == 600
    assert known_cooldown_seconds(_429(), {"reset_at": time.time() + 900}) == pytest.approx(900, abs=5)
    assert known_cooldown_seconds(_429(), {"reset_at": time.time() - 5}) is None
    assert known_cooldown_seconds(_429(), None) is None


@pytest.mark.parametrize("platform, threshold, retry_after, reason, expected", [
    ("cron", 120, 600, FailoverReason.rate_limit, True),
    ("cron", 120, 30, FailoverReason.rate_limit, False),      # short throttle: waiting is cheaper
    ("cron", 120, None, FailoverReason.rate_limit, False),    # cooldown not stated: ordinary backoff
    ("cron", 0, 600, FailoverReason.rate_limit, False),       # disabled
    ("telegram", 120, 600, FailoverReason.rate_limit, False),  # chat surfaces keep waiting
    ("cron", 120, 600, FailoverReason.server_error, False),   # only rate limits
])
def test_should_fail_fast_gate(platform, threshold, retry_after, reason, expected):
    assert should_fail_fast(_agent(platform, threshold), _classified(reason), _429(retry_after), None) is expected


def _settle(monkeypatch, agent, api_error):
    terminal = {"final_response": None, "failed": True}
    slept = []
    monkeypatch.setattr(turn_api_error, "max_retries_exhausted_result", lambda *a, **k: terminal)
    monkeypatch.setattr(turn_api_error, "compute_error_backoff", lambda *a, **k: 600.0)
    monkeypatch.setattr(turn_api_error, "interruptible_backoff_sleep", lambda *a, **k: slept.append(a[1]))
    agent._fallback_index, agent._fallback_chain = 0, []
    verdict = turn_api_error.settle_unrecovered_error(
        agent, api_error=api_error, classified=_classified(), _retry=MagicMock(restart_with_redirected_messages=False),
        status_code=429, error_msg="429", is_context_length_error=False, is_rate_limited=True,
        _is_zai_coding_overload=False, _provider="anthropic", _base="https://api.anthropic.com", _model="m",
        messages=[], api_messages=[], api_kwargs={}, active_system_prompt="", conversation_history=[],
        approx_tokens=0, retry_count=1, max_retries=8, compression_attempts=0, api_call_count=1,
        error_context={},
    )
    return verdict, terminal, slept


@pytest.mark.parametrize("agent_cfg, expected", [({}, 0.0), ({"cron_rate_limit_fail_fast_seconds": 120}, 120.0),
                                                   ({"cron_rate_limit_fail_fast_seconds": "junk"}, 0.0)])
def test_config_key_reaches_the_agent(agent_cfg, expected):
    from unittest.mock import patch
    from run_agent import AIAgent

    cfg = {"agent": agent_cfg}
    with patch("agent.process_bootstrap.OpenAI"), \
         patch("hermes_cli.config.load_config", return_value=cfg), \
         patch("hermes_cli.config.load_config_readonly", return_value=cfg):
        built = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    assert built._cron_rate_limit_fail_fast_s == expected


def test_cron_run_returns_instead_of_sleeping_on_long_cooldown(monkeypatch):
    verdict, terminal, slept = _settle(monkeypatch, _agent(), _429(600))
    assert verdict.action == "return" and verdict.result is terminal
    assert slept == []


def test_chat_surface_still_sleeps_through_the_same_429(monkeypatch):
    verdict, _terminal, slept = _settle(monkeypatch, _agent(platform="telegram"), _429(600))
    assert verdict.action == "fallthrough" and slept == [600.0]
