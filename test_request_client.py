"""Check ambiguous commits, conflicts, error classification and retry budgets."""

import unittest

from request_client import RequestError, Unconfirmed, database_error, run_request


REQUEST_ID = '22222222-2222-4222-8222-222222222222'


class Clock:
    def __init__(self):
        self.elapsed = 0

    def now(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class RequestClientTests(unittest.TestCase):
    def test_committed_request_with_lost_response_is_reused(self):
        class LostResponse:
            def __init__(self):
                self.rows = {}
                self.calls = []

            def submit(self, request_id, body, timeout):
                self.calls.append((request_id, body))
                self.rows.setdefault(request_id, {
                    'request_id': request_id, 'body': body, 'created_at': '2026-11-01T20:00:00Z',
                })
                if len(self.calls) == 1:
                    raise RequestError('connection_lost_after_commit', retryable=True)
                return self.rows[request_id]

        database = LostResponse()
        clock = Clock()
        result = run_request(database, REQUEST_ID, 'order', clock=clock.now, sleep=clock.sleep)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(len(database.rows), 1)
        self.assertEqual(database.calls, [(REQUEST_ID, 'order')] * 2)
        self.assertEqual(result['request']['created_at'], '2026-11-01T20:00:00Z')

    def test_same_key_with_another_body_is_terminal(self):
        class Conflict:
            def submit(self, request_id, body, timeout):
                return {'request_id': request_id, 'body': 'original', 'created_at': 'saved'}

        with self.assertRaisesRegex(RequestError, 'request_id_conflict'):
            run_request(Conflict(), REQUEST_ID, 'changed')

    def test_attempt_limit_does_not_report_failure_as_success(self):
        class Offline:
            calls = 0

            def submit(self, request_id, body, timeout):
                self.calls += 1
                raise RequestError('connection_refused', retryable=True)

        clock, database = Clock(), Offline()
        with self.assertRaisesRegex(Unconfirmed, '3 attempt'):
            run_request(database, REQUEST_ID, 'order', max_attempts=3,
                        clock=clock.now, sleep=clock.sleep)
        self.assertEqual(database.calls, 3)
        self.assertEqual(clock.elapsed, 2)

    def test_total_deadline_caps_request_timeout_and_waits(self):
        clock = Clock()

        class Offline:
            calls = 0

            def submit(self, request_id, body, timeout):
                self.calls += 1
                self.timeout = timeout
                clock.sleep(timeout)
                raise RequestError('client_timeout', retryable=True)

        database = Offline()
        with self.assertRaises(Unconfirmed):
            run_request(database, REQUEST_ID, 'order', deadline=2.5,
                        clock=clock.now, sleep=clock.sleep)
        self.assertEqual(database.calls, 1)
        self.assertEqual(database.timeout, 2.5)
        self.assertEqual(clock.elapsed, 2.5)

    def test_sql_and_permission_errors_are_not_retried(self):
        class Forbidden:
            calls = 0

            def submit(self, request_id, body, timeout):
                self.calls += 1
                raise database_error(3, 'ERROR:  42501\n')

        database = Forbidden()
        with self.assertRaisesRegex(RequestError, '42501'):
            run_request(database, REQUEST_ID, 'order')
        self.assertEqual(database.calls, 1)

    def test_temporary_errors_are_distinguished_from_auth_and_data_errors(self):
        for state in ('08006', '40001', '40P01', '25006', '55P03', '57014', '57P01', '57P03'):
            with self.subTest(state=state):
                self.assertTrue(database_error(3, f'ERROR:  {state}\n').retryable)
        for state in ('28P01', '42501', '42601', '42P01', '23505', '22001'):
            with self.subTest(state=state):
                self.assertFalse(database_error(3, f'ERROR:  {state}\n').retryable)
        self.assertTrue(database_error(2, 'connection refused').retryable)
        self.assertTrue(database_error(2, 'FATAL: the database system is starting up').retryable)
        self.assertFalse(database_error(2, 'FATAL: password authentication failed').retryable)
        self.assertFalse(database_error(1, 'local error').retryable)


if __name__ == '__main__':
    unittest.main()
