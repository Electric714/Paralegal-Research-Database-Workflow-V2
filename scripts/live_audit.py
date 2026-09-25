"""Exercise every registered researcher against real bidders and retain acquisition evidence.

Runs each source in its own process/database so a stalled scraper cannot hide the
remaining sources. No fixtures, mock transports, or automatic approvals are used.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import traceback
from urllib.parse import urlsplit, urlunsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def worker(args):
    from app import database as db
    from app.research.executor import execute_research_run
    from app.research.sources.sam_exclusions import store_uploaded_extract
    import httpx

    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    db.BASE_DIR = directory
    db.DATA_DIR = directory / 'data'
    db.IMPORT_DIR = db.DATA_DIR / 'imports'
    db.DB_PATH = db.DATA_DIR / 'audit.db'
    if args.resume_from:
        db.DATA_DIR.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect((args.resume_from / 'data/audit.db').resolve().as_uri() + '?mode=ro', uri=True) as previous:
            with sqlite3.connect(db.DB_PATH) as current:
                previous.backup(current)
    db.init_db()
    with args.bidder_csv.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        columns = reader.fieldnames
    if args.limit:
        rows = rows[:args.limit]
    if not args.resume_from:
        db.replace_master_database(args.bidder_csv.name, columns, rows, str(args.bidder_csv.resolve()))
    before = db.all_bidder_rows()
    if args.worker == 'sam' and args.sam_extract:
        dataset = store_uploaded_extract(args.sam_extract.read_bytes(), args.sam_extract.name)
        (directory / 'sam-input.json').write_text(json.dumps({'filename': args.sam_extract.name,
            'extract_date': str(dataset.extract_date), 'records': len(dataset.records), 'sha256': dataset.sha256}, indent=2))

    original_send = httpx.Client.send
    def observed_send(client, request, *positional, **kwargs):
        start = time.monotonic()
        parts = urlsplit(str(request.url))
        event = {'method': request.method, 'url': urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))}
        try:
            response = original_send(client, request, *positional, **kwargs)
            event.update(status=response.status_code, content_type=response.headers.get('content-type'))
            if response.is_stream_consumed:
                event.update(bytes=len(response.content), sha256=hashlib.sha256(response.content).hexdigest())
            return response
        except Exception as exc:
            event['error_class'] = type(exc).__name__
            raise
        finally:
            event['elapsed_seconds'] = round(time.monotonic() - start, 3)
            with (directory / 'requests.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event) + '\n')
    httpx.Client.send = observed_send
    run = db.list_runs()[0] if args.resume_from else db.create_run(None, [args.worker], len(rows))
    execute_research_run(run['id'])
    if db.all_bidder_rows() != before:
        raise AssertionError('Live research altered approved bidder values')
    (directory / 'master-preserved.json').write_text('{"preserved": true}')


def inspect(directory):
    path = directory / 'data/audit.db'
    if not path.exists():
        return {}
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        checks = [dict(row) for row in db.execute('''SELECT b.contractor_name, rt.status,
            sc.completeness_status, sc.http_status, sc.acquisition_method, sc.warnings_json
            FROM research_tasks rt JOIN bidders b ON b.id=rt.bidder_id
            LEFT JOIN source_checks sc ON sc.research_task_id=rt.id ORDER BY rt.id''')]
        for check in checks:
            check['warnings'] = json.loads(check.pop('warnings_json') or '[]')
        snapshots = [dict(row) for row in db.execute('SELECT searched_name, source_url, normalized_json FROM evidence_snapshots')]
        for snapshot in snapshots:
            snapshot['payload'] = json.loads(snapshot.pop('normalized_json'))
        return {'checks': checks, 'snapshots': snapshots,
                'evidence_count': db.execute('SELECT count(*) FROM evidence_records').fetchone()[0],
                'proposal_count': db.execute('SELECT count(*) FROM proposed_changes').fetchone()[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bidder-csv', type=Path, required=True)
    parser.add_argument('--sam-extract', type=Path)
    parser.add_argument('--source', action='append', default=[])
    parser.add_argument('--limit', type=int, default=0, help='0 tests every imported bidder')
    parser.add_argument('--timeout', type=int, default=240, help='Maximum seconds per source')
    parser.add_argument('--resume-from', type=Path, help='Resume an interrupted single-source audit directory')
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    parser.add_argument('--output', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return 0
    from app.sources import SOURCES
    from app.research.source_registry import implemented_source_keys
    unknown = set(args.source) - {s['key'] for s in SOURCES}
    if unknown:
        parser.error(f'Unknown sources: {sorted(unknown)}')
    if args.resume_from and len(args.source) != 1:
        parser.error('--resume-from requires exactly one --source')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    output = ROOT / '.runtime/live-audits' / run_id
    output.mkdir(parents=True)
    report = {'run_id': run_id, 'started_at': datetime.now(timezone.utc).isoformat(),
              'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'working_tree': subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True),
              'bidder_csv': str(args.bidder_csv.resolve()), 'bidder_limit': args.limit,
              'status': 'running', 'sources': [], 'artifacts': str(output)}
    def save():
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        (output.parent / 'latest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    def run_source(source):
        key = source['key']
        directory = output / key
        directory.mkdir()
        start = time.monotonic()
        result = {'source': key, 'name': source['name'], 'directory': str(directory)}
        if key not in implemented_source_keys():
            return {**result, 'status': 'not_implemented', 'note': 'No registered acquisition adapter; no research attempted.'}
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', key, '--output', str(directory),
                   '--bidder-csv', str(args.bidder_csv.resolve()), '--limit', str(args.limit)]
        if args.sam_extract:
            command += ['--sam-extract', str(args.sam_extract.resolve())]
        if args.resume_from:
            command += ['--resume-from', str(args.resume_from.resolve())]
            result['resumed_from'] = str(args.resume_from.resolve())
        print(f'START {key}', flush=True)
        with (directory / 'worker.log').open('w', encoding='utf-8') as log:
            try:
                completed = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout)
                result.update(exit_code=completed.returncode, status='finished' if completed.returncode == 0 else 'worker_failed')
            except subprocess.TimeoutExpired:
                result['status'] = 'timed_out'
            except Exception:
                result.update(status='worker_failed', error=traceback.format_exc())
        result.update(inspect(directory))
        result['master_preserved'] = (directory / 'master-preserved.json').exists()
        result['elapsed_seconds'] = round(time.monotonic() - start, 2)
        counts = {}
        for check in result.get('checks', []):
            label = check['status'] + '/' + str(check['completeness_status'])
            counts[label] = counts.get(label, 0) + 1
        result['outcome_counts'] = counts
        print(f'END {key}: {result["status"]} {counts}', flush=True)
        return result
    save()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_source, s) for s in SOURCES if not args.source or s['key'] in args.source]
        for future in as_completed(futures):
            report['sources'].append(future.result())
            save()
    report['status'] = 'audit_complete'  # Completion means measured, not all sources passed.
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    print(f'Report: {output / "report.json"}', flush=True)
    good = {'SUCCESS_NO_MATCH', 'SUCCESS_COMPLETE', 'SUCCESS_WITH_FINDINGS'}
    return 0 if all(s['status'] == 'finished' and s.get('checks') and all(
        c['status'] in good and c['completeness_status'] == 'COMPLETE' for c in s['checks']) for s in report['sources']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
