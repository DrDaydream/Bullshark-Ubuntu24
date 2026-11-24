import unittest
from dataclasses import replace

from benchmark.adversary_sim import Delivery, Event, simulate


class AdversarySimulationTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(nodes=4, faulty_ids=[1], delayed_receivers=[3],
                             start_round=2, rounds=2, delay_ms=10)
        self.event = Event(time_ms=0, round=2, sender=1, receiver=3,
                           marked=True, size_bytes=8)

    def run_events(self, events, mode='A3', **options):
        return simulate(events, mode=mode, **{**self.settings, **options})

    def test_sender_recipient_mark_and_half_open_round_window(self):
        events = [replace(self.event, round=1), self.event,
                  replace(self.event, sender=2), replace(self.event, marked=False),
                  replace(self.event, receiver=2), replace(self.event, round=3),
                  replace(self.event, round=4)]
        direct = [Delivery(i, 0, 'direct') for i in [0, 2, 3, 4, 6]]
        delayed = [Delivery(i, 10, 'delayed') for i in [1, 5]]
        self.assertEqual(self.run_events(events), direct + delayed)
        direct = [Delivery(i, 0, 'direct') for i in [0, 3, 4, 6]]
        delayed = [Delivery(i, 10, 'delayed') for i in [1, 2, 5]]
        self.assertEqual(self.run_events(events, mode='A5'), direct + delayed)
        self.assertEqual(self.run_events(events, mode='A5'), direct + delayed)

    def test_per_sender_budget_and_early_delivery_on_overflow(self):
        events = [self.event, self.event, replace(self.event, sender=2),
                  replace(self.event, sender=2, size_bytes=1),
                  replace(self.event, time_ms=10)]
        expected = [Delivery(1, 0, 'capacity'), Delivery(3, 0, 'capacity'),
                    Delivery(0, 10, 'delayed'), Delivery(2, 10, 'delayed'),
                    Delivery(4, 20, 'delayed')]
        self.assertEqual(self.run_events(events, mode='A5', max_pending_messages=1),
                         expected)
        self.assertEqual(self.run_events(events, mode='A5', max_pending_bytes=8),
                         expected)

    def test_open_ended_round_window_and_empty_trace(self):
        self.assertEqual(self.run_events([], rounds=0), [])
        self.assertEqual(self.run_events([replace(self.event, round=100)], rounds=0),
                         [Delivery(0, 10, 'delayed')])

    def test_orca_configuration_limits(self):
        maximum = (1 << 64) - 1
        for options in ({'start_round': maximum, 'rounds': 1},
                        {'start_round': maximum + 1, 'rounds': 0},
                        {'delay_ms': 86_400_001}, {'faulty_ids': [1, 1]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_events([], **options)
        self.assertEqual(self.run_events([], start_round=maximum, rounds=0,
                                         delay_ms=86_400_000), [])

    def test_seed_selects_the_same_half_of_honest_receivers_in_a_round(self):
        events = [replace(self.event, receiver=i) for i in range(4)]
        rows = self.run_events(events, delayed_receivers=None, seed=7)
        delayed = [row.input_index for row in rows if row.action == 'delayed']
        self.assertEqual(len(delayed), 2)
        self.assertNotIn(1, delayed)
        self.assertEqual(rows, self.run_events(events, delayed_receivers=None, seed=7))

    def test_invalid_virtual_trace_and_settings(self):
        for changes in [dict(time_ms=-1), dict(round=0), dict(sender=4),
                        dict(receiver=-1), dict(marked=1), dict(size_bytes=-1)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_events([replace(self.event, **changes)])
        for changes in [dict(nodes=0), dict(faulty_ids=[4]),
                        dict(delayed_receivers=[False]), dict(rounds=-1),
                        dict(max_pending_bytes=0), dict(max_pending_messages=0),
                        dict(delay_ms=0), dict(start_round=0), dict(mode='A1')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.run_events([], **changes)
        with self.assertRaises(ValueError):
            self.run_events([replace(self.event, time_ms=1), self.event])
