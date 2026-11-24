import unittest
from multiprocessing.pool import ThreadPool
from unittest.mock import patch

from benchmark.logs import LogParser


def summary(commits, sent=(), received=(), replica_commits=()):
    # Each tuple contains a batch or sample ID and its timestamp in seconds.
    primary = ('Header size set 1000\nMax header delay set 200\n'
               'Garbage collection depth set 50\nSync retry delay set 10000\n'
               'Sync retry nodes set 3\nBatch size set 500000\n'
               'Max batch delay set 200\nbooted on 127.0.0.1\n')
    for digest in ('a', 'b', 'c', 'd', 'pending'):
        primary += f'[1970-01-01T00:00:01Z INFO] Created B1(k) -> {digest}=\n'
    replica = primary
    for digest, ts in commits:
        primary += f'[1970-01-01T00:00:{ts}Z INFO] Committed B1(k) -> {digest}=\n'
    for digest, ts in replica_commits:
        replica += f'[1970-01-01T00:00:{ts}Z INFO] Committed B1(k) -> {digest}=\n'
    client = ('Transactions size: 512\nTransactions rate: 10\n'
              '[1970-01-01T00:00:00Z INFO] Start benchmark\n')
    for tx, ts in sent:
        client += f'[1970-01-01T00:00:{ts}Z INFO] sample transaction {tx}\n'
    worker = 'booted on 127.0.0.1\n'
    for tx, digest in received:
        worker += f'Batch {digest}= contains sample tx {tx}\n'
    with patch('benchmark.logs.Pool', lambda: ThreadPool(1)):
        return LogParser([client], [primary, replica], [worker]).result().splitlines()


class LatencyPercentileTests(unittest.TestCase):
    def test_consensus_batches_and_committed_e2e_samples(self):
        lines = summary([('a', '09'), ('c', '03'), ('b', '05'), ('d', '09')],
                        [(1, '00.500'), (2, '00.250'), (3, '00'), (4, '01.250'), (5, '00')],
                        [(1, 'a'), (2, 'a'), (3, 'c'), (4, 'd'), (5, 'pending')],
                        replica_commits=[('a', '02')])
        for line in (' Consensus latency: 3,750 ms', ' Consensus latency P50: 2,000 ms',
                     ' Consensus latency P99: 8,000 ms', ' End-to-end latency: 3,500 ms',
                     ' End-to-end latency P50: 1,750 ms', ' End-to-end latency P99: 7,750 ms'):
            self.assertIn(line, lines)

    def test_empty_populations(self):
        lines = summary([])
        for label in ('Consensus', 'End-to-end'):
            self.assertIn(f' {label} latency: 0 ms', lines)
            self.assertIn(f' {label} latency P50: N/A', lines)
            self.assertIn(f' {label} latency P99: N/A', lines)

    def test_single_zero_sample(self):
        lines = summary([('a', '02')], [(1, '02')], [(1, 'a')])
        for percentile in (50, 99):
            self.assertIn(f' Consensus latency P{percentile}: 1,000 ms', lines)
            self.assertIn(f' End-to-end latency P{percentile}: 0 ms', lines)

    def test_p99_excludes_one_outlier_in_a_hundred(self):
        sent = [(i, '01') for i in range(100)]
        received = [(i, 'a') for i in range(99)] + [(99, 'b')]
        lines = summary([('a', '02'), ('b', '09')], sent, received)
        self.assertIn(' End-to-end latency P99: 1,000 ms', lines)
