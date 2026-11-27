"""Publish the station's existing receive logs to its configured mobile gateway."""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.request import Request, urlopen

import fusion


RUN_ID = re.compile(r'[A-Za-z0-9._-]{1,64}')
LEGACY_RUN_ID = re.compile(r'[0-9a-fA-F]{16}')
MAX_BODY = 32 * 1024 * 1024


def legacy_start_ms(run, tx_rows):
    if not LEGACY_RUN_ID.fullmatch(run):
        return None
    first_ns = min((row['utc_ns'] for row in tx_rows
                    if isinstance(row, dict) and row.get('run') == run
                    and type(row.get('utc_ns')) is int and row['utc_ns'] > 0), default=None)
    return first_ns // 1_000_000 if first_ns is not None else None


def publish(station_dir, legacy_only=False):
    settings = json.loads((station_dir / 'windows.json').read_text(encoding='utf-8-sig'))
    base = str(settings.get('gateway_url', '')).rstrip('/')
    token = str(settings.get('gateway_token', ''))
    if not base.startswith('https://') or len(token) < 32:
        raise SystemExit('Set the HTTPS gateway address and PC token in the station settings first.')

    logs = station_dir / settings.get('logs', 'logs')
    runs = defaultdict(list)
    for row in fusion.read_log(logs / 'rx.jsonl'):
        if not isinstance(row, dict):
            continue
        run = row.get('run')
        if (isinstance(run, str) and RUN_ID.fullmatch(run) and row.get('link') in fusion.LINKS
                and isinstance(row.get('seq'), int) and isinstance(row.get('mono_ns'), int)):
            if legacy_only and not LEGACY_RUN_ID.fullmatch(run):
                continue
            runs[run].append(row)

    published = 0
    for run, rows in sorted(runs.items(), reverse=True):
        tx_path = logs / f'tx-{run}.jsonl'
        tx = list(fusion.read_log(tx_path)) if tx_path.is_file() else []
        started_ms = legacy_start_ms(run, tx)
        if legacy_only and started_ms is None:
            continue
        enriched_rows = list(fusion.arrival_details(rows, tx))
        if started_ms is not None:
            enriched_rows[0]['run_started_utc_ms'] = started_ms
        payload = {'run': run, 'rows': enriched_rows}
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        if len(body) > MAX_BODY:
            raise SystemExit(f'Run {run} exceeds the 32 MiB upload limit.')
        request = Request(base + '/api/v1/desktop-data', data=body, method='POST',
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urlopen(request, timeout=120) as response:
            result = json.load(response)
        if result.get('rows') != len(rows):
            raise SystemExit(f'Gateway did not confirm the full archive for {run}.')
        published += 1
        print(f'{published}/{len(runs)} {run}: {len(rows)} records', flush=True)
    print(f'Published {published} historical experiments.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--station-dir', type=Path, required=True)
    parser.add_argument('--legacy-only', action='store_true',
                        help='Only publish legacy run IDs with a timestamp from the matching transmit log.')
    args = parser.parse_args()
    publish(args.station_dir, legacy_only=args.legacy_only)
