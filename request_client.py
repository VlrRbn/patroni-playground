"""Finish one idempotent database request through HAProxy with bounded retries."""

import argparse
import json
import re
import subprocess
import sys
import time
import uuid

from workload import Database, positive_float


# stdin makes these separate autocommitted statements. The SELECT gets a new
# snapshot after a concurrent INSERT has resolved its unique-key conflict.
REQUEST_SQL = """
INSERT INTO public.lab_requests (request_id, body)
VALUES (:'request_id'::uuid, :'body')
ON CONFLICT (request_id) DO NOTHING;
SELECT json_build_object(
    'request_id', request_id, 'body', body, 'created_at', created_at)
FROM public.lab_requests WHERE request_id = :'request_id'::uuid;
"""
RETRY_STATES = {'25006', '40001', '40P01', '55P03', '57014', '57P01', '57P02', '57P03'}


class RequestError(RuntimeError):
    def __init__(self, reason, retryable=False):
        super().__init__(reason)
        self.retryable = retryable


class Unconfirmed(RuntimeError):
    pass


def database_error(returncode, stderr):
    """Keep diagnostics free of SQL/payload/password; prefer server SQLSTATE."""
    match = re.search(r'(?:ERROR|FATAL|PANIC):\s+([0-9A-Z]{5})\s*$', stderr, re.MULTILINE)
    if match:
        state = match.group(1)
        return RequestError(f'SQLSTATE {state}', state.startswith('08') or state in RETRY_STATES)
    # libpq startup failures don't expose SQLSTATE through psql. Locale is C.
    # Server rejections (including authentication) are terminal unless the
    # diagnostic explicitly identifies a temporary server startup/shutdown.
    if 'FATAL:' in stderr:
        temporary = any(message in stderr for message in (
            'the database system is starting up',
            'the database system is shutting down',
            'the database system is in recovery mode',
        ))
        return RequestError('connection_rejected', temporary)
    return RequestError(f'psql_exit_{returncode}', returncode == 2)


class RequestDatabase(Database):
    def submit(self, request_id, body, timeout):
        env = self.env.copy()
        env.update(LC_ALL='C', PGCLIENTENCODING='UTF8', PGAPPNAME='patroni-request')
        env['PGOPTIONS'] += ' -c lc_messages=C'
        # psql quotes :'body' as an SQL literal. Never interpolate body into SQL.
        command = [
            'psql', '-XAtqw', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=sqlstate',
            '-v', f'request_id={request_id}', '-v', f'body={body}',
            '-h', self.host, '-p', str(self.port), '-U', 'postgres', '-d', 'postgres',
        ]
        try:
            result = subprocess.run(
                command, input=REQUEST_SQL, env=env, text=True,
                capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise RequestError('client_timeout', retryable=True) from error
        if result.returncode:
            raise database_error(result.returncode, result.stderr)
        if not result.stdout.strip():
            raise RequestError('result_missing', retryable=True)
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise RequestError('invalid_response') from error


def run_request(database, request_id, body, *, max_attempts=30, deadline=45,
                retry_delay=1, progress=None, clock=time.monotonic, sleep=time.sleep):
    ends_at = clock() + deadline
    last_error = 'no_attempt'
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        remaining = ends_at - clock()
        if remaining <= 0:
            break
        attempts = attempt
        try:
            row = database.submit(request_id, body, min(6, remaining))
            if (not isinstance(row, dict) or row.get('request_id') != request_id
                    or not isinstance(row.get('body'), str)
                    or not isinstance(row.get('created_at'), str)):
                raise RequestError('invalid_response')
            if row['body'] != body:
                raise RequestError('request_id_conflict: saved body differs')
            return {'status': 'confirmed', 'attempts': attempt, 'request': row}
        except RequestError as error:
            if not error.retryable:
                raise
            last_error = str(error)
            if progress:
                progress(attempt, last_error)
        remaining = ends_at - clock()
        if attempt < max_attempts and remaining > 0:
            sleep(min(retry_delay, remaining))
    raise Unconfirmed(f'not confirmed after {attempts} attempt(s): {last_error}')


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError('Expected a positive integer.')
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request_id', type=uuid.UUID, help='Keep this UUID for every retry.')
    parser.add_argument('--body', required=True, help='Keep the original payload for every retry.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=55432)
    parser.add_argument('--max-attempts', type=positive_int, default=30)
    parser.add_argument('--deadline', type=positive_float, default=45, help='Total budget in seconds.')
    parser.add_argument('--retry-delay', type=positive_float, default=1, help='Pause between attempts.')
    args = parser.parse_args()
    request_id = str(args.request_id)
    if '\0' in args.body:
        parser.error('body cannot contain NUL characters')
    print(f'Request: {request_id}', file=sys.stderr, flush=True)
    try:
        database = RequestDatabase(args.host, args.port)
        result = run_request(
            database, request_id, args.body, max_attempts=args.max_attempts,
            deadline=args.deadline, retry_delay=args.retry_delay,
            progress=lambda attempt, reason: print(
                f'Attempt {attempt}: {reason}; no confirmed result yet',
                file=sys.stderr, flush=True,
            ),
        )
    except (RequestError, Unconfirmed, OSError, ValueError, KeyboardInterrupt) as error:
        print(f'Error: {error or "interrupted"}', file=sys.stderr)
        print('No confirmed result. A row may already exist; keep the same request_id and body.',
              file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
