import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmark.adversary import launch_parameters, resolve_adversary, write_run_snapshot
from benchmark.utils import PathMaker


class AdversaryParameterTests(unittest.TestCase):
    defaults = dict(nodes=[10, 20], rate=[10000, 50000], faults=0, duration=300,
                    tx_size=512, workers=1, collocate=True, runs=2)

    def test_default_and_a0(self):
        self.assertEqual(launch_parameters(self.defaults), self.defaults)
        self.assertEqual(launch_parameters(self.defaults, faults='3')['faults'], 3)
        params = launch_parameters(self.defaults, adversary='A0')
        self.assertEqual(params['adversary']['mode'], 'none')
        self.assertEqual(params['adversary']['byzantine_ids'], [])

    def test_a1_resolves_count_for_each_committee_and_preserves_explicit_ids(self):
        config = launch_parameters(self.defaults, adversary='A1', byzantine='3')['adversary']
        for n in (10, 20):
            keys = [f'key{i}' for i in range(n)]
            resolved = resolve_adversary(config, keys)
            self.assertEqual(resolved['byzantine_authorities'], keys[-3:])
            self.assertEqual(resolved['byzantine_ids'], list(range(n - 3, n)))
        fixed = launch_parameters(self.defaults, nodes='10', adversary='silent',
                                  byzantine_ids='1, 4')['adversary']
        self.assertEqual(resolve_adversary(fixed, list('abcdefghij'))['byzantine_ids'], [1, 4])

    def test_rejects_inconsistent_and_oversized_fault_sets(self):
        for kwargs in (dict(adversary='A1'), dict(adversary='A1', byzantine=4),
                       dict(adversary='A0', byzantine=1), dict(byzantine_ids='1'),
                       dict(adversary='A1', byzantine_ids='10'),
                       dict(adversary='A1', byzantine_ids='1,1'),
                       dict(adversary='A1', byzantine=1, byzantine_ids='2'),
                       dict(adversary='A1', byzantine=1, faults=1)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                launch_parameters(self.defaults, **kwargs)

    def test_local_proposal_sets_reproduce_across_key_generation_and_committee_sizes(self):
        for size, count in ((10, 3), (20, 6)):
            names = [f'key{i}' for i in range(size)]
            def resolve(seed, names):
                params = launch_parameters(self.defaults, adversary='A3', seed=seed)
                return resolve_adversary(params['adversary'], names)['delayed_launch_ids']
            ids = resolve(1, names)
            self.assertEqual(len(set(ids)), count)
            self.assertEqual(ids, resolve(1, list(reversed(names))))
            self.assertNotEqual(ids, resolve(2, names))

    def test_proposal_snapshots_and_result_paths_distinguish_models(self):
        names = [f'key{i}' for i in range(20)]
        folders = set()
        for mode, options, expected in [
            ('A3', dict(byzantine_ids='1,7'), [1, 7]),
            ('A3', dict(byzantine_ids='2,7'), [2, 7]),
            ('A5', {}, list(range(20))),
        ]:
            params = launch_parameters(self.defaults, nodes='20', adversary=mode, **options)
            config = resolve_adversary(params['adversary'], names)
            with TemporaryDirectory() as folder:
                file = write_run_snapshot(folder, params, {}, names, config, 20, 50000, 1,
                                          protocol='bullshark')
                snapshot = json.loads(Path(file).read_text())
            self.assertEqual(snapshot['fault_model'], 'local-proposal')
            self.assertEqual(snapshot['active_launch_ids'], list(range(20)))
            self.assertEqual(snapshot['inactive_launch_ids'], [])
            self.assertEqual(snapshot['offered_rate_tps'], 50000)
            self.assertEqual(config['delayed_launch_ids'], expected)
            self.assertEqual(config['delayed_authorities'], [names[i] for i in expected])
            self.assertEqual((config['seed'], config['delay_ms']), (1, 200))
            self.assertEqual(config['omitted_regular_leader_slots'], 'every second, starting at slot 2')
            folders.add(Path(PathMaker.result_file(0, 20, 1, True, 50000, 512, config)).parent)
        self.assertEqual(len(folders), 3)
        self.assertNotIn(Path('results') / 'A0', folders)

    def test_only_proposal_modes_accept_timing_and_a5_always_delays_all(self):
        for mode in ('A3', 'A5'):
            params = launch_parameters(self.defaults, adversary=mode, delay_ms=86_400_000)
            self.assertEqual(params['adversary']['delay_ms'], 86_400_000)
            with self.assertRaises(ValueError):
                launch_parameters(self.defaults, adversary=mode, delay_ms=86_400_001)
        for kwargs in (dict(seed=0), dict(adversary='A0', delay_ms=0),
                       dict(adversary='A1', byzantine=1, seed=1),
                       dict(adversary='A3', seed=-1), dict(adversary='A3', delay_ms=2**64),
                       dict(adversary='A5', byzantine_ids='1'), dict(adversary='A5', byzantine=2),
                       dict(adversary='A3', byzantine=1, faults=1)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                launch_parameters(self.defaults, **kwargs)
