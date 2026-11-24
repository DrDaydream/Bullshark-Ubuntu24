import unittest

from benchmark.adversary import launch_parameters, resolve_adversary


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
