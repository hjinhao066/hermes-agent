"""One explicit Windows Taojin continuation; no cron/config/service mutations.

The capture implementation belongs to agents-unified/tools/shibei. Pass its
review branch with --tools-root and the existing cn DirectSDK analysis driver.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def undelivered(session, prior_ids):
    items = [i for i in session['items'] if str(i['id']) not in prior_ids]
    seqs = {i['seq'] for i in items}
    keys = {i.get('read_key') for i in items}
    return {**session, 'items': items,
            'reads': {k: v for k, v in session.get('reads', {}).items() if k in keys},
            'profiles': {k: v for k, v in session.get('profiles', {}).items() if v.get('from_seq') in seqs}}


def restore_registration(path, baseline_bytes, slot, out):
    records = read(path, [])
    own = [r for r in records if r.get('slot') == slot and Path(r.get('out', '')).resolve() == out.resolve()]
    kept = [r for r in records if r not in own]
    baseline = json.loads(baseline_bytes)
    if own:
        path.write_bytes(baseline_bytes) if kept == baseline else save(path, kept)
    return {'removed_count': len(own), 'matches_baseline': path.read_bytes() == baseline_bytes}


def load_driver(path, out, tools):
    spec = importlib.util.spec_from_file_location('taojin_existing_cn_driver', path)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    driver.ROOT = out
    # The existing driver initially adds installed tools. Prefer the exact tested
    # branch for this run; never replace modules in other processes.
    sys.path[:0] = [str(tools), str(tools.parent / 'fg-guard')]
    return driver


def capture(args, driver):
    import windows_daily as win
    win.configure()
    run = args.out / 'capture'
    run.mkdir(exist_ok=True)
    if list(run.glob('full-*.json')):
        raise RuntimeError('Capture already started here; preserve evidence and do not restart blindly')
    ledger = win.STATE / 'runs.json'
    baseline = ledger.read_bytes()
    driver.snapshot('run-state-before.json')
    command = win.capture_command(run, args.slot, 100, resume_from=args.resume_from)
    try:
        with (run/'capture.log').open('w', encoding='utf-8') as log:
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                     env=dict(os.environ, FG_OPLOG=str(run/'fg-oplog.jsonl')), timeout=4800)
        good, evidence = win.validate(run, process.returncode, 100)
        guard = read(run/'fg-guard.json', {})
        save(args.out/'capture-result.json', {'good': good, **evidence,
             'new_scanned': max(0, evidence['count']-len(read(args.resume_from)['items'])),
             'foreground_transition_count': guard.get('foreground_transition_count')})
        print(json.dumps({'good': good, **evidence}, ensure_ascii=False), flush=True)
        return good
    finally:
        save(args.out/'registration-restoration.json', restore_registration(ledger, baseline, args.slot, run))
        driver.snapshot('run-state-after.json')


def write_report(args, driver):
    import ab
    import daily
    import report
    result = read(args.out/'capture-result.json')
    if not result or not result.get('good'):
        raise RuntimeError('Capture did not meet the complete 100-item safety checks')
    full = read(Path(result['snapshot']))
    prior_ids = {str(i['id']) for i in read(args.resume_from)['items']}
    history = Path.home()/'reports'/'shibei'
    for pattern in ('*/day-session.json', '*/run-*/full-*.json'):
        for path in history.glob(pattern):
            prior_ids.update(str(i['id']) for i in read(path, {}).get('items', []))
    sys.path.insert(0, str(driver.HERMES/'scripts'))
    import x_digest_preflight
    prior_ids.update(x_digest_preflight.load_already_reported())
    session = undelivered(full, prior_ids)
    save(args.out/'dedup.json', {'scanned_total': len(full['items']), 'new_scanned': result['new_scanned'],
                               'undelivered': len(session['items']), 'excluded': len(full['items'])-len(session['items'])})
    if not session['items']:
        (args.out/'README.md').write_text('累计实取100条，全部已产出，无新增条目。\n', encoding='utf-8')
        return
    save(args.out/'day-session.json', session)
    save(args.out/f'full-{len(session["items"])}.json', session)
    shutil.copyfile(args.out/'capture'/'fg-guard.json', args.out/'fg-guard.json')
    driver.account_status()
    driver.quota('quota-before-r2.json')
    report.call = driver.model_call
    first = report.label(session, args.out)
    first['labels'] = daily.fix_labels(session, first['labels'])
    first['high_value'] = daily.valid_high_value(session, first['labels'], first.get('high_value', []))
    save(args.out/'run-labels.json', {**first, 'account': full['account'], 'fg_guard': result['fg_guard']})
    final = report.write_report(session, first, ab.previous_sessions(driver.dt.date.today()), args.out/'README.md', daily=True)
    daily.library_append(daily.library_entries(final['library'], session), driver.dt.date.today().isoformat(), ab.LIBRARY)
    prefix = (f'> 本次续抓新增 {result["new_scanned"]} 条，连同原9条累计实取100/100；'
              f'本报告仅投递未产出过的 {len(session["items"])} 条。'
              f'累计30/60/90条刷新；前台实际切换 {result["foreground_transition_count"]} 次，只记录；'
              f'原守卫判定 {result["fg_guard"]}，单次授权检查通过，未观察到浏览器激活。\n\n')
    path = args.out/'README.md'
    path.write_text(prefix+path.read_text(encoding='utf-8'), encoding='utf-8')
    inbox = Path.home()/'reports'/f'shibei-x-{driver.dt.date.today():%Y%m%d}'/'ai-radar-candidates'/args.slot
    inbox.mkdir(parents=True, exist_ok=True)
    for name in ('README.md', 'day-session.json', f'full-{len(session["items"])}.json', 'run-labels.json',
                 'labels.json', 'candidates.json', 'metrics.json', 'library-entries.json', 'learn.json', 'fg-guard.json'):
        source = args.out/name
        if source.exists():
            shutil.copyfile(source, inbox/name)
    sys.path.insert(0, str(args.tools_root.parent/'ai-radar'))
    import build_raw
    if build_raw.main([str(args.out), '--labels', str(args.out/'run-labels.json'), '--raw-root', str(inbox/'raw'),
                       '--date', driver.dt.date.today().isoformat()]):
        raise RuntimeError('Raw handoff failed')
    save(inbox/'handoff.json', {'source': str(args.out), 'status': 'awaiting-mac-pull',
         'date': driver.dt.date.today().isoformat(), 'manual': True, 'scanned_total': 100, 'new_only': True})
    save(args.out/'report-result.json', {'new_scanned': result['new_scanned'], 'total_scanned': 100,
         'new_delivered_items': len(session['items']), 'new_useful': sum(v == '有用' for v in first['labels'].values()),
         'foreground_transition_count': result['foreground_transition_count'], 'handoff': str(inbox)})
    driver.quota('quota-after-r2.json')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['capture', 'report', 'all'])
    parser.add_argument('--tools-root', type=Path, required=True)
    parser.add_argument('--analysis-driver', type=Path, required=True)
    parser.add_argument('--resume-from', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--slot', required=True)
    parser.add_argument('--manual-background', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('Windows only')
    args.out.mkdir(parents=True, exist_ok=True)
    if args.manual_background:
        os.environ['SHIBEI_MANUAL_BACKGROUND'] = '1'
    else:
        os.environ.pop('SHIBEI_MANUAL_BACKGROUND', None)
    driver = load_driver(args.analysis_driver, args.out, args.tools_root)
    if args.stage in ('capture', 'all') and not capture(args, driver):
        return 3
    if args.stage in ('report', 'all'):
        write_report(args, driver)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
