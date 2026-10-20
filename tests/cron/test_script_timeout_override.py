"""Per-script budgets must cover long bridge calls without relaxing other jobs."""

import pytest


@pytest.fixture
def script_env(tmp_path, monkeypatch):
    from cron import scheduler

    home = tmp_path / "home"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    (home / "config.yaml").write_text(
        "cron:\n  script_timeout_seconds: 1\n"
        "  script_timeout_overrides:\n    hermes_music_batch.py: 4\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_CRON_SCRIPT_TIMEOUT", raising=False)
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: home)
    return scripts


@pytest.mark.parametrize("absolute", [False, True])
def test_music_bridge_can_finish_after_global_deadline(script_env, absolute):
    from cron.scheduler_script import _run_job_script

    script = script_env / "hermes_music_batch.py"
    script.write_text("import time\ntime.sleep(2)\nprint('bridge finished')\n")
    path = str(script) if absolute else script.name
    ok, output = _run_job_script(path)
    assert ok, output
    assert output == "bridge finished"


def test_other_scripts_keep_global_deadline(script_env):
    from cron.scheduler_script import _run_job_script

    script = script_env / "other.py"
    script.write_text("import time\ntime.sleep(10)\n")
    ok, output = _run_job_script(script.name)
    assert not ok
    assert "timed out after 1s" in output


def test_override_uses_relative_path_not_basename(script_env):
    from cron.scheduler_script import _get_script_timeout_for_path

    assert _get_script_timeout_for_path(
        script_env / "nested" / "hermes_music_batch.py") == 1


def test_override_does_not_leak_between_profiles(script_env, monkeypatch, tmp_path):
    from cron import scheduler
    from cron.scheduler_script import _get_script_timeout_for_path

    home_a = script_env.parent
    home_b = tmp_path / "other-profile"
    (home_b / "scripts").mkdir(parents=True)
    (home_b / "config.yaml").write_text(
        "cron:\n  script_timeout_seconds: 1\n"
        "  script_timeout_overrides:\n    hermes_music_batch.py: 7\n",
        encoding="utf-8",
    )
    for home, expected in ((home_a, 4), (home_b, 7), (home_a, 4)):
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: home)
        assert _get_script_timeout_for_path(
            home / "scripts" / "hermes_music_batch.py") == expected


@pytest.mark.parametrize("override", [0, -1, "invalid", None])
def test_invalid_override_keeps_global_deadline(script_env, monkeypatch, override):
    from cron import scheduler, scheduler_script

    monkeypatch.setattr(scheduler, "load_config", lambda: {
        "cron": {"script_timeout_seconds": 1,
                 "script_timeout_overrides": {"hermes_music_batch.py": override}},
    })
    assert scheduler_script._get_script_timeout_for_path(
        script_env / "hermes_music_batch.py") == 1


def test_remote_failure_is_not_success(script_env):
    from cron.scheduler_script import _run_job_script

    script = script_env / "hermes_music_batch.py"
    script.write_text("import sys\nprint('bridge failed')\nsys.exit(7)\n")
    ok, output = _run_job_script(script.name)
    assert not ok
    assert "code 7" in output
    assert "bridge failed" in output
