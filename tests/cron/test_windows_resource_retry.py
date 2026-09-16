from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cron import scheduler_script as scripts


@pytest.mark.windows_only
@pytest.mark.parametrize('first_code, expected_calls', [(0xC0000142, 2), (1, 1)])
def test_script_retries_only_preinitialization_failure(tmp_path, monkeypatch, first_code, expected_calls):
    script = tmp_path / 'job.py'
    script.write_text('print("done")', encoding='utf-8')
    monkeypatch.setattr(scripts, '_resolve_script_path', lambda _: (script, None))
    monkeypatch.setattr(scripts, '_script_argv', lambda _: (['python', str(script)], {}, None))
    monkeypatch.setattr(scripts, '_get_script_timeout', lambda: 30)
    monkeypatch.setattr(scripts, '_SCRIPT_RESOURCE_RETRY_DELAYS', (0,))
    first = SimpleNamespace(returncode=first_code, communicate=lambda **kw: ('', ''))
    second = SimpleNamespace(returncode=0, communicate=lambda **kw: ('done', ''))
    spawn = Mock(side_effect=[first, second])
    monkeypatch.setattr(scripts.subprocess, 'Popen', spawn)
    ok, output = scripts._run_job_script(str(script))
    assert spawn.call_count == expected_calls
    assert ok is (expected_calls == 2)
    assert output == 'done' if ok else 'Script exited with code 1' == output
