import base64
import json
import shlex
import unittest
from contextlib import ExitStack
from multiprocessing.pool import ThreadPool
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from benchmark.adversary import launch_parameters, resolve_adversary
from benchmark.commands import CommandMaker
from benchmark.config import Key, LocalCommittee
from benchmark.local import LocalBench
from benchmark.logs import LogParser, ParseError
from benchmark.remote import Bench
from benchmark.utils import PathMaker


class CrashLauncherTests(unittest.TestCase):
    def setUp(self):
        self.names = [base64.b64encode(bytes([i]) * 32).decode() for i in range(10)]
        self.config = dict(mode='A1', fault_model='crash-stop', byzantine_ids=[1, 5, 8])
        self.active = [0, 2, 3, 4, 6, 7, 9]
        self.committee = LocalCommittee(self.names, 3000, 2)
        self.params = dict(faults=0, nodes=10, rate=35001, tx_size=512, duration=1, workers=2)
        self.node_params = dict(header_size=1000, max_header_delay=200, gc_depth=50,
                                sync_retry_delay=10000, sync_retry_nodes=3, batch_size=500000,
                                max_batch_delay=200)

    def assert_active_processes(self, launches):
        expected = {f'primary-{i}' for i in self.active}
        expected |= {f'{role}-{i}-{w}' for role in ('client', 'worker')
                     for i in self.active for w in range(2)}
        self.assertEqual(len(launches), len(expected))
        self.assertEqual({name for name, _ in launches}, expected)
        rows = self.committee.workers_addresses()
        ready = [addr for i in self.active for _, addr in rows[i]]
        for name, command in launches:
            if name.startswith('client'):
                self.assertEqual(command.split('--nodes ')[1].split(), ready)
                self.assertIn('--rate 1751 ', command)
                self.assertNotIn('--silence-schedule', command)
            elif name.startswith('primary'):
                self.assertIn('BULLSHARK_FAULTS=0 ', command)

    def remote_commands(self, config):
        launcher = object.__new__(Bench)
        with patch.object(launcher, 'kill'), patch.object(launcher, '_background_run') as start, \
             patch('benchmark.remote.sleep'), patch('benchmark.remote.progress_bar', lambda rows, **_: rows):
            launcher._run_single(35001, self.committee, SimpleNamespace(**self.params),
                                 SimpleNamespace(json=self.node_params), adversary_config=config)
        return [(Path(log).stem, cmd) for (_, cmd, log), _ in start.call_args_list]

    def local_commands(self, options):
        with ExitStack() as stack:
            for target in ('subprocess.run', 'sleep', 'LocalBench._kill_nodes', 'Key.print',
                           'NodeParameters.print', 'LocalCommittee.print', 'LogParser.process'):
                stack.enter_context(patch('benchmark.local.' + target))
            stack.enter_context(patch('benchmark.local.write_run_snapshot', create=True))
            stack.enter_context(patch('benchmark.local.Key.from_file',
                                      side_effect=[Key(name, '') for name in self.names]))
            stack.enter_context(patch('benchmark.local.mkdtemp', return_value='unused'))
            start = stack.enter_context(patch('benchmark.local.LocalBench._background_run'))
            LocalBench(dict(self.params, adversary=options), self.node_params).run()
        return [(Path(log).stem, cmd) for (cmd, log), _ in start.call_args_list]

    def test_remote_crash_uses_only_surviving_endpoints(self):
        self.assert_active_processes(self.remote_commands(self.config))

    def test_local_crash_uses_only_surviving_endpoints(self):
        self.assert_active_processes(self.local_commands(dict(mode='A1', byzantine_ids=[1, 5, 8])))

    def test_both_launchers_profile_each_primary_and_keep_all_workers_and_clients(self):
        self.names = [base64.b64encode(bytes([i]) * 32).decode() for i in range(20)]
        self.committee = LocalCommittee(self.names, 3000, 2)
        self.params['nodes'] = 20
        expected = {f'primary-{i}' for i in range(20)}
        expected |= {f'{role}-{i}-{w}' for role in ('client', 'worker') for i in range(20) for w in range(2)}
        for mode, extra, expected_ids in [('A3', {'byzantine_ids': '1,7'}, [1, 7]),
                                          ('A5', {}, list(range(20)))]:
            options = launch_parameters(self.params, adversary=mode, seed='12', delay_ms='0', **extra)['adversary']
            resolved = resolve_adversary(options, self.names)
            for commands in (self.local_commands(options), self.remote_commands(resolved)):
                self.assertEqual(len(commands), len(expected))
                self.assertEqual({name for name, _ in commands}, expected)
                for name, command in commands:
                    tokens = shlex.split(command)
                    env = dict(t.split('=', 1) for t in tokens if t.startswith('BULLSHARK_'))
                    if name.startswith('primary'):
                        index = int(name.split('-')[1])
                        self.assertEqual(env['BULLSHARK_FAULTS'], '0')
                        self.assertEqual(json.loads(env['BULLSHARK_BENCH_PROPOSAL_PROFILE']),
                                         dict(mode=mode, launch_id=index, seed=12, delay_ms=0,
                                              delayed=index in expected_ids))
                    else:
                        self.assertNotIn('BULLSHARK_BENCH_PROPOSAL_PROFILE', env)
                    if name.startswith('client'):
                        self.assertEqual(len(tokens[tokens.index('--nodes')+1:]), 40)
                        self.assertNotIn('--silence-schedule', tokens)

    def test_remote_tmux_quoting_preserves_json_for_the_primary(self):
        launcher = object.__new__(Bench)
        launcher.connect = {}
        command = '''BULLSHARK_BENCH_PROPOSAL_PROFILE='{"mode":"A5","seed":7}' ./node'''
        with patch('benchmark.remote.Connection') as connection:
            connection.return_value.run.return_value.stderr = ''
            launcher._background_run('localhost', command, 'primary-0.log')
        tmux_command = shlex.split(connection.return_value.run.call_args.args[0])[-1]
        profile = shlex.split(tmux_command)[0].split('=', 1)[1]
        self.assertEqual(json.loads(profile), {'mode': 'A5', 'seed': 7})

    def test_baseline_primaries_clear_any_inherited_proposal_profile(self):
        for config in (None, dict(mode='none'), self.config):
            command = CommandMaker.run_primary('keys', 'committee', 'db', 'params', adversary_config=config)
            self.assertIn('env -u BULLSHARK_BENCH_PROPOSAL_PROFILE ./node ', command)

    def test_live_and_archived_proposal_summary_counts_all_active_authorities(self):
        for mode in ('A3', 'A5'):
            options = launch_parameters(self.params, adversary=mode)['adversary']
            config = resolve_adversary(options, self.names)
            with TemporaryDirectory() as folder, patch('benchmark.logs.Pool', lambda: ThreadPool(1)):
                path = Path(folder)
                self.write_logs(path, range(10))
                first = LogParser.process(folder, adversary=config, committee_size=10)
                archived = LogParser.process(folder, faults='?')
                for result in (first, archived):
                    self.assertEqual((result.faults, result.committee_size, result.workers), (0, 10, 2))
                    self.assertIn(f'Local proposal mode: {mode}', result.result())
                    self.assertIn('Active authorities: 10', result.result())
                    self.assertIn('Input rate: 35,020 tx/s', result.result())
                (path / 'primary-0.log').unlink()
                with self.assertRaises(ParseError):
                    LogParser.process(folder, faults='?')

    def test_legacy_faults_still_launch_all_processes_with_original_schedules(self):
        launcher = object.__new__(Bench)
        with patch.object(launcher, 'kill'), patch.object(launcher, '_background_run') as start, \
             patch('benchmark.remote.sleep'), patch('benchmark.remote.progress_bar', lambda rows, **_: rows):
            params = SimpleNamespace(**dict(self.params, faults=3))
            launcher._run_single(35001, self.committee, params, SimpleNamespace(json=self.node_params))
        self.assertEqual(len(start.call_args_list), 50)
        for (_, cmd, log), _ in start.call_args_list:
            if Path(log).stem.startswith('primary'):
                self.assertIn('BULLSHARK_FAULTS=3 ', cmd)
            elif Path(log).stem.startswith('client'):
                self.assertIn('--silence-schedule', cmd)

    def write_logs(self, path, ids):
        client = ('Transactions size: 512\nTransactions rate: 1751\n'
                  '[2025-01-01T00:00:00Z INFO] Start benchmark\n')
        primary = ('Header size set 1000\nMax header delay set 200\nGarbage collection depth set 50\n'
                   'Sync retry delay set 10000\nSync retry nodes set 3\nBatch size set 500000\n'
                   'Max batch delay set 200\nbooted on 127.0.0.1\n')
        for i in ids:
            (path / f'primary-{i}.log').write_text(primary)
            for w in range(2):
                (path / f'client-{i}-{w}.log').write_text(client)
                (path / f'worker-{i}-{w}.log').write_text('booted on 127.0.0.1\n')

    def test_archived_crash_run_keeps_original_committee_and_offered_rate(self):
        with TemporaryDirectory() as folder, patch('benchmark.logs.Pool', lambda: ThreadPool(1)):
            path = Path(folder)
            self.write_logs(path, self.active)
            first = LogParser.process(folder, adversary=self.config, committee_size=10)
            archived = LogParser.process(folder, faults='?')
            for result in (first, archived):
                self.assertEqual((result.faults, result.committee_size, result.workers), (3, 10, 2))
                self.assertIn('Input rate: 24,514 tx/s', result.result())
                self.assertIn('End-to-end latency P99: N/A', result.result())
            (path / 'worker-0-1.log').unlink()
            with self.assertRaises(ParseError):
                LogParser.process(folder)

    def test_remote_summaries_and_snapshots_are_separate_by_mode_and_crash_ids(self):
        def folder(faults, **mode):
            return Path(PathMaker.result_file(faults, 10, 2, True, 35001, 512, **mode)).parent

        legacy = folder(0)
        a0 = folder(0, adversary=dict(mode='none'))
        a1 = folder(3, adversary=self.config)
        other_ids = folder(3, adversary=dict(self.config, byzantine_ids=[7, 8, 9]))
        self.assertEqual(legacy, Path('results'))
        self.assertEqual(len({legacy, a0, a1, other_ids}), 4)
        self.assertEqual(a1, folder(3, adversary=dict(self.config, byzantine_ids=[8, 1, 5])))
