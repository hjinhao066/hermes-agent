"""``apply_retry_restarts`` bounds the refunding restarts (#106108).

The redirect and rebuilt-for-fallback paths refund the iteration budget and re-issue the
iteration; nothing else in the turn loop counts them, so a flag that keeps re-arming
(a request cancelled on every attempt) refunded forever and held the turn lease.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.turn_iteration_prep import apply_retry_restarts, begin_iteration
from agent.turn_retry_state import TurnRetryState

RESTART_FLAGS = ["restart_with_redirected_messages", "restart_with_rebuilt_messages"]
MAX_RETRIES = 3


def _apply(agent, flag: str, restart_count: int):
    _retry = TurnRetryState()
    setattr(_retry, flag, True)
    return apply_retry_restarts(
        agent, _retry=_retry, response=None, interrupted=False, messages=[],
        conversation_history=[], user_message="hi", api_kwargs={}, current_turn_user_idx=0,
        final_response=None, retry_count=0, max_retries=MAX_RETRIES, api_call_count=1,
        restart_count=restart_count, length_continue_retries=0,
        _preflight_compression_blocked=True, _turn_exit_reason="unknown",
    )


def _agent():
    budget = SimpleNamespace(refunds=0)
    budget.refund = lambda: setattr(budget, "refunds", budget.refunds + 1)
    agent = SimpleNamespace(iteration_budget=budget, steered=[])
    agent._drain_pending_redirect = lambda: "last correction"
    agent.steer = agent.steered.append
    return agent


@pytest.mark.parametrize("flag", RESTART_FLAGS)
def test_single_restart_still_reissues_the_iteration(flag):
    """A lone correction / fallback activation keeps its refund-and-continue contract."""
    agent = _agent()
    verdict = _apply(agent, flag, restart_count=0)
    assert verdict.action == "continue"
    assert (agent.iteration_budget.refunds, verdict.api_call_count) == (1, 0)
    if flag == "restart_with_rebuilt_messages":
        assert verdict._preflight_compression_blocked is False  # still the single consumer


@pytest.mark.parametrize("flag", RESTART_FLAGS)
def test_restart_refunds_are_bounded_per_turn(flag):
    """Re-arming the flag every iteration breaks after ``max_retries`` refunds instead of
    refunding forever (the turn ends, so the session turn lease is released)."""
    agent = _agent()
    restart_count, verdicts = 0, []
    while len(verdicts) < MAX_RETRIES + 5 and (not verdicts or verdicts[-1].action != "break"):
        verdicts.append(_apply(agent, flag, restart_count))
        restart_count = verdicts[-1].restart_count
    assert [v.action for v in verdicts] == ["continue"] * MAX_RETRIES + ["break"]
    assert agent.iteration_budget.refunds == MAX_RETRIES
    assert verdicts[-1]._turn_exit_reason.endswith("restart_limit_exceeded")
    # The correction that tripped the redirect cap is handed back as the next user turn.
    assert agent.steered == (["last correction"] if flag == "restart_with_redirected_messages" else [])


def _interrupted_agent(tool_interrupt_reason):
    agent = SimpleNamespace(
        _interrupt_requested=True, _tool_interrupt_reason=tool_interrupt_reason, quiet_mode=True,
        _drain_pending_redirect=lambda: None, _checkpoint_mgr=SimpleNamespace(new_turn=lambda: None),
    )
    return begin_iteration(
        agent, messages=[], conversation_history=[], original_user_message="hi",
        api_call_count=0, interrupted=False, _turn_exit_reason="unknown",
    )


@pytest.mark.parametrize("tool_interrupt_reason, expected", [
    # Human stops keep the historical reason (every ``interrupt()`` category, and a bare flag write).
    ("explicit stop requested", "interrupted_by_user"),
    ("user sent a new message", "interrupted_by_user"),
    (None, "interrupted_by_user"),
    # A producer that named itself via ``tool_reason`` is booked as the issuer (#112647).
    ("cron inactivity watchdog", "interrupted_by_system(cron_inactivity_watchdog)"),
    ("turn liveness watchdog", "interrupted_by_system(turn_liveness_watchdog)"),
])
def test_interrupt_exit_reason_names_the_system_issuer(tool_interrupt_reason, expected):
    """A watchdog abort must not be recorded as a user stop: the exit reason carries the issuer."""
    verdict = _interrupted_agent(tool_interrupt_reason)
    assert (verdict.action, verdict.interrupted, verdict._turn_exit_reason) == ("break", True, expected)

def test_forward_fallback_hops_are_not_charged_against_the_retry_cap():
    """A configured chain longer than ``api_max_retries`` must still be requested.

    Charging every provider switch against the per-call retry cap stopped the noon
    mail job on hop 4 of a 9-entry chain, before the next model was called.
    """
    agent = _agent()
    agent._fallback_chain = [{"model": f"m{i}"} for i in range(6)]
    watermark, restart_count = 0, 0
    actions = []
    for index in range(1, 7):
        agent._fallback_index = index
        _retry = TurnRetryState()
        _retry.restart_with_rebuilt_messages = True
        verdict = apply_retry_restarts(
            agent, _retry=_retry, response=None, interrupted=False, messages=[],
            conversation_history=[], user_message="hi", api_kwargs={}, current_turn_user_idx=0,
            final_response=None, retry_count=0, max_retries=MAX_RETRIES, api_call_count=1,
            restart_count=restart_count, fallback_restart_watermark=watermark,
            length_continue_retries=0, _preflight_compression_blocked=True,
            _turn_exit_reason="unknown",
        )
        actions.append(verdict.action)
        watermark = verdict.fallback_restart_watermark
        restart_count = verdict.restart_count
    assert actions == ["continue"] * 6
    assert (restart_count, watermark, agent.iteration_budget.refunds) == (0, 6, 6)


def test_a_stalled_fallback_index_still_hits_the_restart_cap():
    """Re-arming the rebuild flag without moving the chain index stays bounded."""
    agent = _agent()
    agent._fallback_chain = [{"model": "m0"}, {"model": "m1"}]
    agent._fallback_index = 2
    restart_count, watermark, verdicts = 0, 2, []
    while len(verdicts) < MAX_RETRIES + 2 and (not verdicts or verdicts[-1].action != "break"):
        _retry = TurnRetryState()
        _retry.restart_with_rebuilt_messages = True
        verdicts.append(apply_retry_restarts(
            agent, _retry=_retry, response=None, interrupted=False, messages=[],
            conversation_history=[], user_message="hi", api_kwargs={}, current_turn_user_idx=0,
            final_response=None, retry_count=0, max_retries=MAX_RETRIES, api_call_count=1,
            restart_count=restart_count, fallback_restart_watermark=watermark,
            length_continue_retries=0, _preflight_compression_blocked=True,
            _turn_exit_reason="unknown",
        ))
        restart_count = verdicts[-1].restart_count
        watermark = verdicts[-1].fallback_restart_watermark
    assert [v.action for v in verdicts] == ["continue"] * MAX_RETRIES + ["break"]
    assert verdicts[-1]._turn_exit_reason == "rebuilt_restart_limit_exceeded"
    assert watermark == 2
