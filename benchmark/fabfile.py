# Copyright(C) Facebook, Inc. and its affiliates.
from fabric import task
from invoke import Exit
import json
from dataclasses import asdict
from pathlib import Path

from benchmark.adversary import launch_parameters
from benchmark.adversary_sim import Event, simulate
from benchmark.local import LocalBench
from benchmark.logs import ParseError, LogParser
from benchmark.utils import Print
from benchmark.plot import Ploter, PlotError
from benchmark.instance import InstanceManager
from benchmark.remote import Bench, BenchError


MODE_HELP = {
    'adversary': 'A0: unmodified. A1: crash-stop. A3/A5: seeded own-proposal delay and alternating regular leader omission.',
    'byzantine': 'A1: trailing authorities to omit. A3: number of delayed proposers (default (n-1)//3).',
    'byzantine-ids': 'Comma-separated launch IDs to omit (A1) or delay (A3).',
    'seed': 'A3/A5 proposal timing seed; also selects the A3 subset (default 1).',
    'delay-ms': 'A3/A5 maximum local proposal delay in milliseconds, inclusive (default 200).',
}


@task(help=MODE_HELP)
def local(ctx, debug=True, adversary='', byzantine=None, byzantine_ids='',
          nodes=None, rate=None, duration=None, faults=None, seed=None, delay_ms=None):
    ''' Run benchmarks on localhost '''
    bench_params = {
        'faults': 0,
        'nodes': 4,
        'workers': 1,
        'rate': 50_000,
        'tx_size': 512,
        'duration': 20,
    }
    node_params = {
        'header_size': 1000,  # bytes
        'max_header_delay': 200,  # ms
        'gc_depth': 50,  # rounds
        'sync_retry_delay': 10_000,  # ms
        'sync_retry_nodes': 3,  # number of nodes
        'batch_size': 500_000,  # bytes
        'max_batch_delay': 200  # ms
    }
    try:
        bench_params = launch_parameters(bench_params, adversary=adversary, byzantine=byzantine,
                                         byzantine_ids=byzantine_ids, nodes=nodes, rate=rate,
                                         duration=duration, faults=faults, seed=seed, delay_ms=delay_ms)
        for field in ('nodes', 'rate'):
            if isinstance(bench_params[field], list) and len(bench_params[field]) != 1:
                raise ValueError(f'Local benchmarks accept one value for {field}')
    except ValueError as e:
        raise Exit(str(e), code=2) from e
    try:
        ret = LocalBench(bench_params, node_params).run(debug)
        print(ret.result())
    except BenchError as e:
        Print.error(e)


@task
def replay(ctx, trace):
    ''' Replay a JSON fixture of synthetic A3/A5 events in virtual time. '''
    try:
        fixture = json.loads(Path(trace).read_text(encoding='utf-8'))
        events = [Event(**event) for event in fixture['events']]
        deliveries = simulate(events, **fixture['settings'])
    except (OSError, UnicodeError, KeyError, TypeError, ValueError) as e:
        raise Exit(f'Invalid replay fixture: {e}', code=2) from e
    print(json.dumps([asdict(row) for row in deliveries], indent=2))


@task
def create(ctx, nodes=10):
    ''' Create a testbed'''
    try:
        InstanceManager.make().create_instances(nodes)
    except BenchError as e:
        Print.error(e)


@task
def destroy(ctx):
    ''' Destroy the testbed '''
    try:
        InstanceManager.make().terminate_instances()
    except BenchError as e:
        Print.error(e)


@task
def start(ctx, max=2):
    ''' Start at most `max` machines per data center '''
    try:
        InstanceManager.make().start_instances(max)
    except BenchError as e:
        Print.error(e)


@task
def stop(ctx):
    ''' Stop all machines '''
    try:
        InstanceManager.make().stop_instances()
    except BenchError as e:
        Print.error(e)


@task
def info(ctx):
    ''' Display connect information about all the available machines '''
    try:
        InstanceManager.make().print_info()
    except BenchError as e:
        Print.error(e)


@task
def install(ctx):
    ''' Install the codebase on all machines '''
    try:
        Bench(ctx).install()
    except BenchError as e:
        Print.error(e)


@task(help=MODE_HELP)
def remote(ctx, debug=False, adversary='', byzantine=None, byzantine_ids='',
           nodes=None, rate=None, duration=None, faults=None, runs=None, seed=None, delay_ms=None):
    ''' Run benchmarks on AWS '''
    bench_params = {
        'faults': 0,
        'nodes': [10, 20],
        'workers': 1,
        'collocate': True,
        'rate': [10_000, 50_000],
        'tx_size': 512,
        'duration': 300,
        'runs': 2,
    }
    node_params = {
        'header_size': 50,  # bytes
        'max_header_delay': 5_000,  # ms
        'gc_depth': 50,  # rounds
        'sync_retry_delay': 10_000,  # ms
        'sync_retry_nodes': 3,  # number of nodes
        'batch_size': 500_000,  # bytes
        'max_batch_delay': 200  # ms
    }
    try:
        bench_params = launch_parameters(bench_params, adversary=adversary, byzantine=byzantine,
                                         byzantine_ids=byzantine_ids, nodes=nodes, rate=rate,
                                         duration=duration, faults=faults, runs=runs, seed=seed, delay_ms=delay_ms)
    except ValueError as e:
        raise Exit(str(e), code=2) from e
    try:
        Bench(ctx).run(bench_params, node_params, debug)
    except BenchError as e:
        Print.error(e)


@task
def plot(ctx):
    ''' Plot performance using the logs generated by "fab remote" '''
    plot_params = {
        'faults': [0],
        'nodes': [10, 20, 50],
        'workers': [1],
        'collocate': True,
        'tx_size': 512,
        'max_latency': [2_500, 4_500]
    }
    try:
        Ploter.plot(plot_params)
    except PlotError as e:
        Print.error(BenchError('Failed to plot performance', e))


@task
def kill(ctx):
    ''' Stop execution on all machines '''
    try:
        Bench(ctx).kill()
    except BenchError as e:
        Print.error(e)


@task
def logs(ctx):
    ''' Print a summary of the logs '''
    try:
        print(LogParser.process('./logs', faults='?').result())
    except ParseError as e:
        Print.error(BenchError('Failed to parse logs', e))
