"""Check that unconfirmed outcomes never become acknowledged data loss."""

import unittest

from workload import summarize


JournalEvent = dict[str, str | int | float]


def journal(outcomes: list[tuple[int, str, float, float]]) -> list[JournalEvent]:
    events: list[JournalEvent] = [{'event': 'start'}]
    acknowledgements = 0
    for seq, kind, before, after in outcomes:
        events.append({'event': 'attempt', 'seq': seq, 'elapsed_s': before})
        events.append({'event': kind, 'seq': seq, 'elapsed_s': after})
        acknowledgements += kind == 'ack'
    events.append({
        'event': 'finish', 'elapsed_s': 30,
        'attempts': len(outcomes), 'acknowledged': acknowledgements,
    })
    return events


class WorkloadReportTests(unittest.TestCase):
    def test_unconfirmed_rows_are_not_reported_as_acknowledged_loss(self):
        events = journal([
            (1, 'ack', 0, 0.1), (2, 'unconfirmed', 1, 3),
            (3, 'unconfirmed', 4, 6), (4, 'ack', 7, 7.1),
        ])
        result = summarize(events, {1, 2, 4})
        self.assertEqual(result['confirmed_missing_count'], 0)
        self.assertEqual(result['unconfirmed_present_sequences'], [2])
        self.assertEqual(result['unconfirmed_absent_count'], 1)
        self.assertAlmostEqual(result['error_windows'][0]['success_gap_s'], 7)
        self.assertAlmostEqual(result['error_windows'][0]['failure_to_success_s'], 6.1)

    def test_only_missing_acknowledged_ids_count_as_loss(self):
        result = summarize(journal([
            (1, 'ack', 0, 0.1), (2, 'ack', 1, 1.1), (3, 'unconfirmed', 2, 4),
        ]), {1})
        self.assertEqual(result['confirmed_missing_sequences'], [2])
        self.assertEqual(result['unconfirmed_absent_count'], 1)

    def test_no_recovery_is_reported_as_an_open_window(self):
        result = summarize(journal([
            (1, 'ack', 0, 0.1), (2, 'unconfirmed', 2, 4),
        ]), {1})
        self.assertFalse(result['error_windows'][0]['recovered'])
        self.assertEqual(result['error_windows'][0]['observed_until_finish_s'], 28)
        self.assertNotIn('failure_to_success_s', result['error_windows'][0])

    def test_no_ack_does_not_claim_a_zero_second_recovery(self):
        result = summarize(journal([(1, 'unconfirmed', 0, 2)]), set())
        self.assertIsNone(result['max_success_gap_s'])
        self.assertFalse(result['error_windows'][0]['recovered'])

    def test_incomplete_journal_and_duplicate_outcomes_are_rejected(self):
        events = journal([(1, 'ack', 0, 0.1)])
        with self.assertRaisesRegex(ValueError, 'completed journal'):
            summarize(events[:-1], {1})
        events.insert(-1, {'event': 'ack', 'seq': 1, 'elapsed_s': 1})
        with self.assertRaisesRegex(ValueError, 'duplicate outcome'):
            summarize(events, {1})

    def test_missing_outcome_and_inconsistent_counters_are_rejected(self):
        events = journal([(1, 'ack', 0, 0.1)])
        del events[2]
        with self.assertRaisesRegex(ValueError, 'without an outcome'):
            summarize(events, set())
        events = journal([(1, 'ack', 0, 0.1)])
        events[-1]['acknowledged'] = 0
        with self.assertRaisesRegex(ValueError, 'counters'):
            summarize(events, {1})


if __name__ == '__main__':
    unittest.main()
