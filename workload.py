"""Measure acknowledged writes and client-visible gaps through HAProxy."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / '.local' / 'workload'
CREATE_TABLE = '''
CREATE TABLE IF NOT EXISTS public.lab_workload (
    run_id uuid NOT NULL,
    seq bigint NOT NULL,
    payload text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, seq)
)
'''
SETTINGS = '''SELECT json_build_object(
    'server_addr', host(inet_server_addr()),
    'in_recovery', pg_is_in_recovery(),
    'synchronous_commit', current_setting('synchronous_commit'),
    'synchronous_standby_names', current_setting('synchronous_standby_names'),
    'fsync', current_setting('fsync'))'''


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, host, port):
        if shutil.which('psql') is None:
            raise ValueError('Install the PostgreSQL client: psql is required on the host.')
        self.host, self.port = host, port
        self.env = os.environ.copy()
        password = self.env.get('PGPASSWORD')
        if not password:
            for line in (ROOT / '.env').read_text().splitlines():
                if line.startswith('PATRONI_SUPERUSER_PASSWORD='):
                    password = line.split('=', 1)[1]
                    break
        if not password:
            raise ValueError('No local database password found; run make setup.')
        self.env.update(
            PGPASSWORD=password, PGCONNECT_TIMEOUT='2',
            PGTARGETSESSIONATTRS='read-write', PGAPPNAME='patroni-workload',
            PGOPTIONS='-c synchronous_commit=on -c statement_timeout=3000 -c lock_timeout=1000',
        )

    def execute(self, query):
        command = [
            'psql', '-XAtq', '-v', 'ON_ERROR_STOP=1', '-h', self.host,
            '-p', str(self.port), '-U', 'postgres', '-d', 'postgres', '-c', query,
        ]
        try:
            result = subprocess.run(
                command, env=self.env, text=True, capture_output=True, timeout=6,
                # Ctrl+C stops the writer after this bounded request finishes.
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return False, '', 'client_timeout'
        if result.returncode:
            # No password, SQL text or connection string is written to the log.
            return False, '', f'psql_exit_{result.returncode}'
        return True, result.stdout.strip(), None

    def query(self, query):
        ok, output, reason = self.execute(query)
        if not ok:
            raise RuntimeError(f'Database request failed ({reason}); check make status and connectivity.')
        return output


def append_event(journal, event):
    journal.write(json.dumps(event, ensure_ascii=False) + '\n')
    journal.flush()
    os.fsync(journal.fileno())


def write_load(args):
    database = Database(args.host, args.port)
    database.query(CREATE_TABLE)
    settings = json.loads(database.query(SETTINGS))
    if settings['in_recovery'] or settings['fsync'] != 'on':
        raise ValueError('This exercise requires a writable primary with fsync=on.')
    run_id = str(uuid.uuid4())
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f'{run_id}.jsonl'
    stopped = False

    def stop_requested(signum, frame):
        nonlocal stopped
        stopped = True

    previous = {sig: signal.signal(sig, stop_requested) for sig in (signal.SIGINT, signal.SIGTERM)}
    started = time.monotonic()
    attempts, acknowledged = 0, 0
    last_progress = started
    try:
        with path.open('x') as journal:
            append_event(journal, {
                'event': 'start', 'version': 1, 'run_id': run_id, 'at': utc_now(),
                'host': args.host, 'port': args.port, 'duration_s': args.duration,
                'interval_s': args.interval, 'payload_bytes': args.payload_bytes,
                'initial_primary': settings,
            })
            print(f'Run: {run_id}\nJournal: {path}', flush=True)
            while not stopped and time.monotonic() - started < args.duration:
                attempts += 1
                before = time.monotonic()
                append_event(journal, {
                    'event': 'attempt', 'seq': attempts,
                    'elapsed_s': before - started, 'at': utc_now(),
                })
                payload = 'x' * args.payload_bytes
                query = (
                    'INSERT INTO public.lab_workload (run_id, seq, payload) '
                    f"VALUES ('{run_id}'::uuid, {attempts}, '{payload}') RETURNING seq"
                )
                ok, output, reason = database.execute(query)
                elapsed = time.monotonic() - started
                if ok and output == str(attempts):
                    acknowledged += 1
                    event = {'event': 'ack', 'seq': attempts, 'elapsed_s': elapsed}
                else:
                    event = {
                        'event': 'unconfirmed', 'seq': attempts, 'elapsed_s': elapsed,
                        'reason': reason or 'unexpected_response',
                    }
                append_event(journal, event)
                if time.monotonic() - last_progress >= 2:
                    print(f'{elapsed:.1f}s: attempts={attempts} ACK={acknowledged} no_ACK={attempts-acknowledged}', flush=True)
                    last_progress = time.monotonic()
                if not stopped:
                    time.sleep(args.interval)
            append_event(journal, {
                'event': 'finish', 'at': utc_now(), 'elapsed_s': time.monotonic() - started,
                'attempts': attempts, 'acknowledged': acknowledged,
                'stopped_by_signal': stopped,
            })
        print(f'Finished: attempts={attempts} ACK={acknowledged} no_ACK={attempts-acknowledged}', flush=True)
        print(f'Report: python3 workload.py report {path}', flush=True)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def read_journal(argument):
    if argument == 'latest':
        candidates = list(LOG_DIR.glob('*.jsonl'))
        if not candidates:
            raise ValueError('No workload journals found; run make load first.')
        path = max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)
    else:
        path = Path(argument)
    events = [json.loads(line) for line in path.read_text().splitlines()]
    if not events or events[0].get('event') != 'start' or events[-1].get('event') != 'finish':
        raise ValueError('Journal is incomplete. Wait for the writer to finish or stop it with Ctrl+C.')
    uuid.UUID(events[0]['run_id'])
    return path, events


def summarize(events, present):
    if not events or events[0].get('event') != 'start' or events[-1].get('event') != 'finish':
        raise ValueError('A completed journal is required.')
    attempts, outcomes, acknowledged = {}, {}, set()
    windows, pending = [], None
    last_ack = None
    success_gaps = []
    for event in events[1:-1]:
        kind = event.get('event')
        seq = event.get('seq')
        if kind == 'attempt':
            if not isinstance(seq, int) or seq <= 0 or seq in attempts:
                raise ValueError('Invalid or duplicate attempt in journal.')
            attempts[seq] = event['elapsed_s']
            continue
        if kind not in ('ack', 'unconfirmed') or seq not in attempts or seq in outcomes:
            raise ValueError('Invalid or duplicate outcome in journal.')
        outcomes[seq] = kind
        elapsed = event['elapsed_s']
        if kind == 'unconfirmed':
            if pending is None:
                pending = {
                    'last_ack_elapsed_s': last_ack,
                    'first_failed_attempt_elapsed_s': attempts[seq],
                    'unconfirmed_attempts': 0,
                }
            pending['unconfirmed_attempts'] += 1
        else:
            acknowledged.add(seq)
            if last_ack is not None:
                success_gaps.append(elapsed - last_ack)
            if pending is not None:
                pending.update(
                    recovered=True, first_recovered_ack_elapsed_s=elapsed,
                    failure_to_success_s=elapsed-pending['first_failed_attempt_elapsed_s'],
                    success_gap_s=None if last_ack is None else elapsed-last_ack,
                )
                windows.append(pending)
                pending = None
            last_ack = elapsed
    if set(outcomes) != set(attempts):
        raise ValueError('Journal has attempts without an outcome.')
    if events[-1]['attempts'] != len(attempts) or events[-1]['acknowledged'] != len(acknowledged):
        raise ValueError('Journal counters do not match its events.')
    if pending is not None:
        pending.update(
            recovered=False,
            observed_until_finish_s=events[-1]['elapsed_s']-pending['first_failed_attempt_elapsed_s'],
        )
        windows.append(pending)
    unconfirmed = set(attempts) - acknowledged
    missing = acknowledged - present
    return {
        'attempts': len(attempts), 'acknowledged': len(acknowledged),
        'unconfirmed': len(unconfirmed), 'confirmed_missing_count': len(missing),
        'confirmed_missing_sequences': sorted(missing),
        'unconfirmed_present_count': len(unconfirmed & present),
        'unconfirmed_present_sequences': sorted(unconfirmed & present),
        'unconfirmed_absent_count': len(unconfirmed - present),
        'max_success_gap_s': max(success_gaps, default=None),
        'error_windows': windows,
    }


def report_load(args):
    path, events = read_journal(args.journal)
    metadata = events[0]
    run_id = str(uuid.UUID(metadata['run_id']))
    database = Database(args.host or metadata['host'], args.port or metadata['port'])
    primary = json.loads(database.query(SETTINGS))
    if primary['in_recovery']:
        raise ValueError('Report must query the current primary.')
    output = database.query(f"SELECT seq FROM public.lab_workload WHERE run_id='{run_id}'::uuid ORDER BY seq")
    present = {int(line) for line in output.splitlines()}
    report = {
        'run_id': run_id, 'reported_at': utc_now(), 'journal': str(path),
        'started_at': metadata['at'], 'finished_at': events[-1]['at'],
        'stopped_by_signal': events[-1]['stopped_by_signal'],
        'initial_primary': metadata['initial_primary'], 'report_primary': primary,
        **summarize(events, present),
    }
    report_path = path.with_suffix('.report.json')
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    report_path.write_text(rendered)
    print(rendered, end='')
    print(f'Saved: {report_path}', file=sys.stderr)


def positive_float(value):
    parsed = float(value)
    if not 0 < parsed < float('inf'):
        raise argparse.ArgumentTypeError('Expected a finite positive number.')
    return parsed


def payload_size(value):
    parsed = int(value)
    if not 1 <= parsed <= 32768:
        raise argparse.ArgumentTypeError('Expected payload size between 1 and 32768 bytes.')
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    writer = commands.add_parser('write', help='Write through HAProxy and journal outcomes on the host.')
    writer.add_argument('--host', default='127.0.0.1')
    writer.add_argument('--port', type=int, default=55432)
    writer.add_argument('--duration', type=positive_float, default=120)
    writer.add_argument('--interval', type=positive_float, default=0.1)
    writer.add_argument('--payload-bytes', type=payload_size, default=256, metavar='1..32768')
    writer.set_defaults(action=write_load)
    reporter = commands.add_parser('report', help='Compare a completed journal against the current primary.')
    reporter.add_argument('journal', nargs='?', default='latest')
    reporter.add_argument('--host')
    reporter.add_argument('--port', type=int)
    reporter.set_defaults(action=report_load)
    args = parser.parse_args()
    try:
        args.action(args)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f'Error: {error}\n')


if __name__ == '__main__':
    main()
