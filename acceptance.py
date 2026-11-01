"""Exercise a disposable cluster; remove only its own containers and volumes."""

import json
import os
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parent
COMPOSE = [
    'docker', 'compose', '-p', f'patroni-lab-test-{os.getpid()}',
    '-f', str(ROOT / 'compose.yaml'),
    '-f', str(ROOT / 'compose.acceptance.yaml'),
]


def compose(*args, capture=False, check=True):
    return subprocess.run(
        [*COMPOSE, *args], cwd=ROOT, text=True, check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=600,
    )


def sql_result(query, port=5000, *, sqlstate=False):
    return compose(
        'exec', '-T', '-e', f'LAB_SQL_PORT={port}', '-e', f'LAB_SQL={query}',
        '-e', f'LAB_SQL_VERBOSITY={"sqlstate" if sqlstate else "default"}',
        '-e', 'PGOPTIONS=-c statement_timeout=3000 -c lock_timeout=1000',
        'pg3', 'sh', '-c',
        'PGPASSWORD="$PATRONI_SUPERUSER_PASSWORD" PGCONNECT_TIMEOUT=2 '
        'exec psql -XAtw -v ON_ERROR_STOP=1 -v VERBOSITY="$LAB_SQL_VERBOSITY" '
        '-h haproxy -p "$LAB_SQL_PORT" '
        '-U postgres -d postgres -c "$LAB_SQL"',
        capture=True, check=False,
    )


def sql(query, port=5000):
    result = sql_result(query, port)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def postgresql_not_writable(node):
    result = compose(
        'exec', '-T', '-e', 'PGCONNECT_TIMEOUT=2',
        '-e', 'PGOPTIONS=-c statement_timeout=3000', node,
        'psql', '-XAtw', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'postgres',
        '-c', 'SELECT pg_is_in_recovery()', capture=True, check=False,
    )
    if result.returncode == 0:
        if result.stdout.strip() not in ('t', 'f'):
            raise RuntimeError(f'{node}: unexpected recovery state: {result.stdout!r}')
        return result.stdout.strip() == 't'
    # A failed connection alone is insufficient: authenticate/socket errors must
    # not be mistaken for demotion. pg_ctl exit 3 specifically means not running.
    status = compose(
        'exec', '-T', node, 'pg_ctl', '-D', '/var/lib/postgresql/patroni/data',
        'status', capture=True, check=False,
    )
    if status.returncode == 3:
        return True
    raise RuntimeError(f'{node}: SQL check failed while PostgreSQL is not confirmed stopped: '
                       f'{result.stderr.strip()}')


def haproxy_rejects_write():
    # Roll back even the successful positive control, preserving the two rows.
    result = sql_result(
        "BEGIN; INSERT INTO lab_probe VALUES (3, 'quorum write probe'); ROLLBACK",
        sqlstate=True,
    )
    if result.returncode == 0:
        return False
    # psql exit 2 means a broken connection. For SQL errors, accept only a
    # read-only transaction (25006), not a syntax/schema error in this probe.
    if result.returncode == 2 or (
        result.returncode == 3 and 'ERROR:  25006' in result.stderr
    ):
        return True
    raise RuntimeError(f'Unexpected failure of HAProxy write probe: {result.stderr.strip()}')


def members():
    result = compose(
        'exec', '-T', 'pg3', 'patronictl', '-c', '/etc/patroni.yaml',
        'list', '--format=json', capture=True,
    )
    return json.loads(result.stdout)


def leader():
    leaders = [m['Member'] for m in members() if m['Role'] == 'Leader']
    if len(leaders) != 1:
        raise RuntimeError(f'Expected one leader, got {leaders}')
    return leaders[0]


def wait_for(description, predicate, timeout=120):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                print(f'OK: {description}', flush=True)
                return
        except (RuntimeError, subprocess.SubprocessError, ValueError) as error:
            last_error = error
        time.sleep(2)
    raise RuntimeError(f'Timed out: {description}; last error: {last_error}')


def healthy_cluster():
    nodes = members()
    return (
        len(nodes) == 3
        and sum(m['Role'] == 'Leader' for m in nodes) == 1
        and sum(m['Role'] == 'Replica' and m['State'] == 'streaming' for m in nodes) == 2
    )


def exercise_cluster():
    print('[1/5] Start an isolated cluster', flush=True)
    compose('up', '--build', '-d', '--wait', '--wait-timeout', '180')
    wait_for('one leader and two streaming replicas', healthy_cluster)
    wait_for('HAProxy routes writes to primary', lambda: sql('SELECT pg_is_in_recovery()') == 'f')
    sql('CREATE TABLE lab_probe (id integer PRIMARY KEY, note text NOT NULL); '
        "INSERT INTO lab_probe VALUES (1, 'before failover')")
    wait_for('row reaches both replicas', lambda: all(
        compose('exec', '-T', node, 'psql', '-XAt', '-U', 'postgres', '-d', 'postgres',
                '-c', 'SELECT count(*) FROM lab_probe', capture=True).stdout.strip() == '1'
        for node in ('pg1', 'pg2', 'pg3')
    ))
    if sql('SELECT pg_is_in_recovery()', port=5001) != 't':
        raise RuntimeError('Read endpoint did not route to a replica')
    try:
        sql("INSERT INTO lab_probe VALUES (99, 'must fail')", port=5001)
    except RuntimeError as error:
        if 'read-only' not in str(error):
            raise
    else:
        raise RuntimeError('Replica accepted a write')

    print('[2/5] Planned switchover', flush=True)
    if leader() == 'pg1':
        candidate = 'pg2'
    else:
        candidate = 'pg1'
    compose('exec', '-T', 'pg3', 'patronictl', '-c', '/etc/patroni.yaml',
            'switchover', '--candidate', candidate, '--force')
    wait_for('candidate becomes leader', lambda: leader() == candidate)
    wait_for('replicas follow the new timeline', healthy_cluster)

    print('[3/5] Kill the leader without graceful shutdown', flush=True)
    old_leader = leader()
    # Keep pg3 running as the operator/client container throughout the scenario.
    if old_leader == 'pg3':
        raise RuntimeError('Unexpected leader pg3 before failure exercise')
    compose('kill', '-s', 'SIGKILL', old_leader)
    wait_for('automatic election chooses another leader', lambda: leader() != old_leader)
    wait_for('writes resume through the same HAProxy endpoint', lambda: sql(
        "INSERT INTO lab_probe VALUES (2, 'after failover') ON CONFLICT DO NOTHING; "
        'SELECT count(*) FROM lab_probe'
    ).splitlines()[-1] == '2')

    print('[4/5] Restart the former leader', flush=True)
    compose('start', old_leader)
    wait_for('former leader rejoins as a streaming replica', healthy_cluster)
    wait_for('rejoined node receives the new row', lambda: compose(
        'exec', '-T', old_leader, 'psql', '-XAt', '-U', 'postgres', '-d', 'postgres',
        '-c', 'SELECT count(*) FROM lab_probe', capture=True,
    ).stdout.strip() == '2')

    print('[5/5] Lose etcd quorum, then restore it', flush=True)
    quorum_leader = leader()
    if postgresql_not_writable(quorum_leader):
        raise RuntimeError('Expected a writable primary before losing etcd quorum')
    if haproxy_rejects_write():
        raise RuntimeError('Expected writes through HAProxy before losing etcd quorum')
    compose('stop', 'etcd2', 'etcd3')

    def no_primary():
        result = compose('exec', '-T', 'pg3', 'python3', '-c',
                         'import urllib.request\n'
                         'for node in ("pg1", "pg2", "pg3"):\n'
                         ' try:\n'
                         '  urllib.request.urlopen(f"http://{node}:8008/primary", timeout=2)\n'
                         '  raise SystemExit(1)\n'
                         ' except urllib.error.HTTPError as error:\n'
                         '  if error.code != 503: raise\n', capture=True, check=False)
        return result.returncode == 0

    wait_for('all nodes stop advertising a writable primary', no_primary)
    wait_for('former primary is stopped or in recovery',
             lambda: postgresql_not_writable(quorum_leader))
    wait_for('all PostgreSQL nodes are stopped or in recovery', lambda: all(
        postgresql_not_writable(node) for node in ('pg1', 'pg2', 'pg3')
    ))
    wait_for('HAProxy rejects writes while etcd quorum is lost', haproxy_rejects_write)
    compose('start', 'etcd2', 'etcd3')
    wait_for('cluster recovers after etcd quorum is restored', healthy_cluster)
    wait_for('both rows remain readable through primary', lambda: sql('SELECT count(*) FROM lab_probe') == '2')
    wait_for('HAProxy accepts writes after etcd quorum is restored',
             lambda: not haproxy_rejects_write())


def main():
    try:
        exercise_cluster()
        print('PASS: replication, routing, switchover, failover, rejoin, etcd quorum.', flush=True)
    except BaseException:
        compose('logs', '--no-color', '--tail=60', check=False)
        raise
    finally:
        compose('down', '--volumes', '--remove-orphans')


if __name__ == '__main__':
    main()
