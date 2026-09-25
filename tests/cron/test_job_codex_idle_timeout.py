"""A slow email-analysis run must not tune concurrent jobs' stream watchdogs."""
import pytest

from agent.chat_completion_helpers import _resolve_nonstream_watchdogs
from cron.scheduler import _construct_cron_agent, _CronAgentSetup
from run_agent import AIAgent


def make_agent(job):
    setup = _CronAgentSetup(
        model="gpt-5.6-sol",
        runtime={"api_key": "test-only", "provider": "openai-codex",
                 "base_url": "https://chatgpt.com/backend-api/codex",
                 "api_mode": "codex_responses"},
    )
    return _construct_cron_agent(AIAgent, job, {}, setup, workdir=None,
                                 session_id="test-email-timeout", session_db=None)


def test_job_timeout_reaches_watchdog_without_leaking(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS", "12")
    (tmp_path / "config.yaml").write_text("{}", encoding="utf-8")
    agents = [make_agent({"codex_event_stale_timeout_seconds": 120}), make_agent({})]
    try:
        kwargs = {"model": "gpt-5.6-sol", "input": "Read the email batch"}
        tuned, normal = [_resolve_nonstream_watchdogs(a, kwargs) for a in agents]
        assert normal.idle_timeout < 30 < tuned.idle_timeout
        assert tuned.idle_enabled and normal.idle_enabled
        assert tuned.stale_timeout == normal.stale_timeout
        assert tuned.ttfb_timeout == normal.ttfb_timeout
    finally:
        for agent in agents:
            agent.close()


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), "bad"])
def test_invalid_timeout_fails_before_constructing_agent(value):
    def forbidden(**kwargs):
        pytest.fail("Invalid job configuration must not allocate an agent")
    with pytest.raises(ValueError):
        _construct_cron_agent(forbidden, {"codex_event_stale_timeout_seconds": value}, {},
                              _CronAgentSetup(), workdir=None, session_id=None, session_db=None)
