# Copyright(C) Facebook, Inc. and its affiliates.
import json
from os import environ
from os.path import join
from shlex import quote

from benchmark.utils import PathMaker


class CommandMaker:

    @staticmethod
    def cleanup():
        return (
            f'rm -rf .db-* ; rm -f .*.json ; mkdir -p {PathMaker.results_path()}'
        )

    @staticmethod
    def clean_logs():
        return f'rm -rf {PathMaker.logs_path()} ; mkdir -p {PathMaker.logs_path()}'

    @staticmethod
    def compile():
        # remote.py invokes cargo from <repo>/node, while the benchmark
        # aliases expect binaries under <repo>/target/release.
        return 'cargo build --target-dir ../target --quiet --release --features benchmark'

    @staticmethod
    def generate_key(filename):
        assert isinstance(filename, str)
        return f'./node generate_keys --filename {filename}'

    @staticmethod
    def run_primary(keys, committee, store, parameters, debug=False, faults=0,
                    adversary_config=None, launch_id=None):
        assert isinstance(keys, str)
        assert isinstance(committee, str)
        assert isinstance(parameters, str)
        assert isinstance(debug, bool)
        v = '-vvv' if debug else '-vv'
        cmd = (f'./node {v} run --keys {keys} --committee {committee} '
               f'--store {store} --parameters {parameters} primary')
        if adversary_config and adversary_config.get('fault_model') == 'local-proposal':
            if type(launch_id) is not int or launch_id < 0:
                raise ValueError('A proposal profile requires the primary launch ID')
            profile = {k: adversary_config[k] for k in ('mode', 'seed', 'delay_ms')}
            profile.update(launch_id=launch_id, delayed=launch_id in adversary_config['delayed_launch_ids'])
            encoded = quote(json.dumps(profile, separators=(',', ':')))
            cmd = f'BULLSHARK_BENCH_PROPOSAL_PROFILE={encoded} {cmd}'
        else:
            cmd = f'env -u BULLSHARK_BENCH_PROPOSAL_PROFILE {cmd}'
        if adversary_config is not None:
            return (f'BULLSHARK_FAULTS=0 BULLSHARK_ADVERSARY_SEED=0 '
                    f'BULLSHARK_CLIENT_DURING_SILENCE=pause {cmd}')
        adversary_seed = environ.get('BULLSHARK_ADVERSARY_SEED', '0')
        if (not adversary_seed.isascii() or not adversary_seed.isdigit()
                or int(adversary_seed) > 2**64 - 1):
            raise ValueError('BULLSHARK_ADVERSARY_SEED must be an unsigned 64-bit integer')
        client_during_silence = environ.get(
            'BULLSHARK_CLIENT_DURING_SILENCE', 'pause'
        ).lower()
        if client_during_silence not in {'send', 'pause'}:
            raise ValueError(
                'BULLSHARK_CLIENT_DURING_SILENCE must be send or pause'
            )
        return (f'BULLSHARK_FAULTS={faults} '
                f'BULLSHARK_ADVERSARY_SEED={adversary_seed} '
                f'BULLSHARK_CLIENT_DURING_SILENCE={client_during_silence} {cmd}')

    @staticmethod
    def run_worker(keys, committee, store, parameters, id, debug=False):
        assert isinstance(keys, str)
        assert isinstance(committee, str)
        assert isinstance(parameters, str)
        assert isinstance(debug, bool)
        v = '-vvv' if debug else '-vv'
        return (f'./node {v} run --keys {keys} --committee {committee} '
                f'--store {store} --parameters {parameters} worker --id {id}')

    @staticmethod
    def run_client(address, size, rate, nodes, silence_schedule='', silence_slot_ms=0):
        assert isinstance(address, str)
        assert isinstance(size, int) and size > 0
        assert isinstance(rate, int) and rate >= 0
        assert isinstance(nodes, list)
        assert all(isinstance(x, str) for x in nodes)
        assert isinstance(silence_schedule, str)
        assert all(value in {'0', '1'} for value in silence_schedule)
        assert isinstance(silence_slot_ms, int) and silence_slot_ms >= 0
        nodes = f'--nodes {" ".join(nodes)}' if nodes else ''
        silence = (
            f'--silence-schedule {silence_schedule} '
            f'--silence-slot-ms {silence_slot_ms}'
            if silence_schedule else ''
        )
        return (f'./benchmark_client {address} --size {size} --rate {rate} '
                f'{silence} {nodes}')

    @staticmethod
    def kill():
        return 'tmux kill-server'

    @staticmethod
    def alias_binaries(origin):
        assert isinstance(origin, str)
        node, client = join(origin, 'node'), join(origin, 'benchmark_client')
        return f'rm -f node benchmark_client ; ln -s {node} . ; ln -s {client} .'
