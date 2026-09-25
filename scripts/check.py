"""Bounded regression runner. Each invocation owns its server, database and logs."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import traceback
import uuid

ROOT = Path(__file__).resolve().parents[1]


def serve(data: str, port: int) -> None:
    sys.path.insert(0, str(ROOT / 'backend'))
    from app import database as db
    db.BASE_DIR = Path(data)
    db.DATA_DIR = Path(data) / 'data'
    db.IMPORT_DIR = db.DATA_DIR / 'imports'
    db.DB_PATH = db.DATA_DIR / 'check.db'
    import uvicorn
    from app.main_with_wcca import app
    uvicorn.run(app, host='127.0.0.1', port=port)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def workflow(client, output: Path) -> dict:
    def request(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    require('text/html' in request('GET', '/').headers.get('content-type', ''), 'Frontend not served')
    master = (ROOT / 'test_data/regression_businesses.csv').read_bytes()
    preview = request('POST', '/api/import/preview', files={'file': ('bidders.csv', master)}).json()
    require(preview['valid_for_import'], f"Fixture import validation failed: {preview.get('validation_errors')}")
    request('POST', '/api/import', files={'file': ('bidders.csv', master)})
    before = request('GET', '/api/export').text
    filename = datetime.now(timezone.utc).strftime('SAM_Exclusions_Public_Extract_V2_%y%j.CSV')
    research = request('POST', '/api/sources/sam/extract', files={
        'file': (filename, (ROOT / 'test_data/sam_regression.csv').read_bytes())}).json()
    require(request('GET', '/api/export').text == before, 'Research changed master before approval')
    run_id = research['comparison']['run']['id']
    diagnostics = request('GET', f'/api/runs/{run_id}/diagnostics/export').json()
    (output / 'sam-diagnostics.json').write_text(json.dumps(diagnostics, indent=2), encoding='utf-8')
    summary = diagnostics['summary']
    require(summary['integrity_ok'] and summary['safe_complete_tasks'] == 3, 'Incomplete research summary')
    require(summary['counts']['completed'] == 2 and summary['counts']['no_match'] == 1, 'Unexpected SAM outcomes')
    proposals = request('GET', '/api/review').json()['items']
    require(len(proposals) == 2, 'Expected exactly two proposals')
    proposals.sort(key=lambda item: item['bidder_id'])
    for proposal, decision in zip(proposals, ['approved', 'dismissed']):
        outcome = request('POST', f"/api/review/{proposal['id']}", json={
            'decision': decision, 'actor': 'regression-check'}).json()['item']
        require(outcome['decision'] == decision, 'Review decision not persisted')
    rows = list(csv.DictReader(io.StringIO(request('GET', '/api/export').text)))
    values = {row['id']: row['state_federal_debarment'] for row in rows}
    require(values == {'TEST-1': 'Y', 'TEST-2': '', 'TEST-3': 'Y'}, 'Approval/dismissal/no-match corrupted export')
    all_diagnostics = request('GET', '/api/diagnostics/export').json()
    (output / 'paralegal-research-diagnostics.json').write_text(json.dumps(all_diagnostics, indent=2), encoding='utf-8')
    require(not any(d['severity'].upper() in {'ERROR', 'CRITICAL'} for d in all_diagnostics['diagnostics']), 'Error diagnostics present')
    return {'status': 'passed', 'research_run_id': run_id, 'summary': summary}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--npm', help='Optional absolute npm executable path')
    parser.add_argument('--live-source', action='append', default=[])
    parser.add_argument('--bidder-csv', type=Path, default=ROOT / 'Bidder Database-Example.csv')
    parser.add_argument('--serve', help=argparse.SUPPRESS)
    parser.add_argument('--port', type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve, args.port)
        return 0
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8]
    output = ROOT / '.runtime/checks' / run_id
    output.mkdir(parents=True)
    report = {'run_id': run_id, 'started_at': datetime.now(timezone.utc).isoformat(),
              'status': 'failed', 'checks': [], 'live_sources': {'status': 'not_checked'}, 'artifacts': str(output)}
    def command(name, argv, cwd, timeout):
        start = time.monotonic()
        with (output / f'{name}.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        report['checks'].append({'name': name, 'exit_code': result.returncode, 'elapsed_seconds': round(time.monotonic()-start, 2)})
        require(result.returncode == 0, f'{name} failed; see {output / (name + ".log")}')
    try:
        report['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        report['working_tree'] = subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True)
        command('pytest', [sys.executable, '-m', 'pytest', 'tests', '-q', f'--junitxml={output / "pytest.xml"}'], ROOT / 'backend', 300)
        npm = args.npm or next((str(p) for p in (ROOT / '.runtime').glob('node-*/npm.cmd')), None) or shutil.which('npm')
        require(bool(npm), 'npm unavailable; run scripts/start.ps1 -SetupOnly')
        os.environ['PATH'] = str(Path(npm).resolve().parent) + os.pathsep + os.environ['PATH']
        command('frontend-build', [npm, 'run', 'build'], ROOT / 'frontend', 180)
        import httpx
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        with (output / 'server.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve', str(output / 'sandbox'), '--port', str(port)], stdout=log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=120, trust_env=False) as client:
                    deadline = time.monotonic() + 30
                    while True:
                        require(process.poll() is None, 'Server exited during startup')
                        try:
                            if client.get('/api/health', timeout=1).json().get('status') == 'ok':
                                break
                        except (httpx.HTTPError, ValueError):
                            pass
                        require(time.monotonic() < deadline, 'Server startup timed out')
                        time.sleep(.2)
                    report['workflow'] = workflow(client, output)
                    if args.live_source:
                        response = client.post('/api/import', files={'file': (args.bidder_csv.name, args.bidder_csv.read_bytes())})
                        response.raise_for_status()
                        original = client.get('/api/export').text
                        live = []
                        report['live_sources'] = {'status': 'running', 'results': live}
                        bidders = client.get('/api/bidders').json()['items']
                        report['live_sources']['imported_bidder_count'] = response.json()['row_count']
                        report['live_sources']['tested_bidders'] = [b['contractor_name'] for b in bidders[:2]]
                        for source in args.live_source:
                            print(f'Checking live source: {source}', flush=True)
                            response = client.post('/api/runs', json={'source_keys': [source], 'bidder_ids': [b['_internal_id'] for b in bidders[:2]]})
                            response.raise_for_status()
                            run = response.json()['item']['id']
                            response = client.get(f'/api/runs/{run}/diagnostics/export')
                            response.raise_for_status()
                            payload = response.json()
                            (output / f'live-{source}.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
                            live.append({'source': source, 'summary': payload['summary'], 'diagnostics': payload['diagnostics']})
                        require(client.get('/api/export').text == original, 'Live research modified master')
                        report['live_sources']['status'] = 'passed' if all(r['summary']['attention_tasks'] == 0 and r['summary']['integrity_ok'] for r in live) else 'needs_attention'

            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        report['status'] = 'passed' if report['live_sources']['status'] in {'not_checked', 'passed'} else 'needs_attention'
    except Exception:
        report['error'] = traceback.format_exc()
        print(report['error'], file=sys.stderr)
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        serialized = json.dumps(report, indent=2)
        (output / 'report.json').write_text(serialized, encoding='utf-8')
        latest = ROOT / '.runtime/checks/latest.json'
        staging = output / 'latest.tmp'
        staging.write_text(serialized, encoding='utf-8')
        staging.replace(latest)
        print(f"{report['status']}: {latest}", flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
