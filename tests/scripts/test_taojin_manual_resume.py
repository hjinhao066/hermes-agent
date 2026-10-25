import importlib.util
import json
from pathlib import Path


spec = importlib.util.spec_from_file_location('taojin_resume', Path(__file__).resolve().parents[2]/'scripts'/'taojin_manual_resume.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_delivery_excludes_previously_sent_posts_and_attached_reads():
    session = {'items': [{'id': 'old', 'seq': 1, 'read_key': 'old'}, {'id': 'new', 'seq': 10, 'read_key': 'new'}],
               'reads': {'old': {'text': 'sent'}, 'new': {'text': 'fresh'}},
               'profiles': {'old': {'from_seq': 1}, 'new': {'from_seq': 10}}}
    fresh = runner.undelivered(session, {'old'})
    assert [i['id'] for i in fresh['items']] == ['new']
    assert set(fresh['reads']) == set(fresh['profiles']) == {'new'}
    assert len(session['items']) == 2


def test_registration_restore_preserves_other_session_changes(tmp_path):
    ledger = tmp_path/'runs.json'
    out = tmp_path/'capture'
    baseline = b'[]'
    foreign = {'slot': 'foreign', 'out': str(tmp_path/'other')}
    ledger.write_text(json.dumps([foreign, {'slot': 'ours', 'out': str(out)}]), encoding='utf-8')
    assert runner.restore_registration(ledger, baseline, 'ours', out)['removed_count'] == 1
    assert runner.read(ledger) == [foreign]
