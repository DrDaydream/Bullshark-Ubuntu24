"""Resolve Bullshark benchmark modes to the processes and proposals they affect."""

import json
from datetime import datetime, timezone
from pathlib import Path
from random import Random


def _integer(value, name, minimum):
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise ValueError(f'{name} must be an integer')
    if parsed < minimum:
        raise ValueError(f'{name} must be at least {minimum}')
    return parsed


def _numbers(value, name, minimum):
    if isinstance(value, str):
        value = value.split(',')
    elif not isinstance(value, (list, tuple)):
        value = [value]
    return [_integer(item, name, minimum) for item in value]


def validate_adversary(options, nodes, faults):
    if options is None:
        return None
    if not isinstance(options, dict):
        raise ValueError('Adversary configuration must be a dictionary')
    if faults:
        raise ValueError('Explicit modes require --faults 0')
    mode = {'a0': 'none', 'none': 'none', 'a1': 'silent', 'silent': 'silent',
            'a3': 'A3', 'a5': 'A5'}.get(
        str(options.get('mode', '')).strip().lower())
    if mode is None:
        raise ValueError('Select A0, A1, A3 or A5')
    raw_ids = options.get('byzantine_ids', [])
    if not isinstance(raw_ids, (list, tuple)):
        raise ValueError('byzantine_ids must be a list of launch IDs')
    ids = [_integer(i, 'byzantine ID', 0) for i in raw_ids]
    raw_count = options.get('byzantine_count', options.get('crash_last'))
    count = _integer(raw_count, 'byzantine', 0) if raw_count is not None else 0
    if len(ids) != len(set(ids)) or any(i >= min(nodes) for i in ids):
        raise ValueError('Fixed authority IDs must be distinct and present in every committee')
    if ids and (count or (mode in ('A3', 'A5') and raw_count is not None)):
        raise ValueError('Choose --byzantine or --byzantine-ids')
    if max(count, len(ids)) > (min(nodes) - 1) // 3:
        raise ValueError('Too many selected authorities for the smallest committee')
    if mode in ('A3', 'A5'):
        if mode == 'A5' and (raw_count is not None or ids):
            raise ValueError('A5 delays all proposers; a subset is only available in A3')
        config = dict(mode=mode, fault_model='local-proposal', byzantine_ids=ids)
        for field, default in [('seed', 1), ('delay_ms', 200)]:
            value = _integer(options.get(field, default), field, 0)
            if value >= 2**64:
                raise ValueError(f'{field} must fit in an unsigned 64-bit integer')
            config[field] = value
        if config['delay_ms'] > 86_400_000:
            raise ValueError('delay_ms cannot exceed one day (86,400,000 ms)')
        if raw_count is not None:
            config['byzantine_count'] = count
        return config
    if 'seed' in options or 'delay_ms' in options:
        raise ValueError('Only A3 and A5 accept --seed and --delay-ms')
    if mode == 'none':
        if count or ids:
            raise ValueError('A0 requires an empty crash set')
        return dict(mode=mode, byzantine_ids=[])
    if not (count or ids):
        raise ValueError('A1 requires --byzantine or --byzantine-ids')
    config = dict(mode=mode, fault_model='crash-stop', byzantine_ids=ids)
    if count:
        config['crash_last'] = count
    return config


def launch_parameters(defaults, *, nodes=None, rate=None, duration=None, runs=None,
                      faults=None, adversary='', byzantine=None, byzantine_ids='',
                      seed=None, delay_ms=None):
    params = dict(defaults)
    if nodes is not None:
        params['nodes'] = _numbers(nodes, 'nodes', 2)
    if rate is not None:
        params['rate'] = _numbers(rate, 'rate', 0)
    for name, value, minimum in [('duration', duration, 1), ('runs', runs, 1), ('faults', faults, 0)]:
        if value is not None:
            params[name] = _integer(value, name, minimum)
    sizes = _numbers(params['nodes'], 'nodes', 2)
    if not sizes or params['faults'] >= min(sizes):
        raise ValueError('Require at least one committee and fewer faults than nodes')
    count = _integer(byzantine, 'byzantine', 0) if byzantine is not None else None
    ids = _numbers(byzantine_ids, 'byzantine ID', 0) if byzantine_ids else []
    if not adversary:
        if seed is not None or delay_ms is not None:
            raise ValueError('Only A3 and A5 accept --seed and --delay-ms')
        if count or ids:
            raise ValueError('Select A1 or A3 when specifying a subset')
        return params
    opts = dict(mode=adversary, byzantine_ids=ids, byzantine_count=count)
    if seed is not None:
        opts['seed'] = seed
    if delay_ms is not None:
        opts['delay_ms'] = delay_ms
    params['adversary'] = validate_adversary(opts, sizes, params['faults'])
    return params


def resolve_adversary(options, launch_authorities):
    """Map zero-based launch IDs to full keys, preserving committee insertion order."""
    if options is None:
        return None
    names = list(launch_authorities)
    if not all(isinstance(name, str) and name for name in names) or len(set(names)) != len(names):
        raise ValueError('Launch authority keys must be nonempty and unique')
    ids = options['byzantine_ids']
    if any(i < 0 or i >= len(names) for i in ids):
        raise ValueError('Byzantine launch ID is outside this committee')
    if options.get('fault_model') == 'local-proposal':
        if options['mode'] == 'A5':
            ids = list(range(len(names)))
        elif not ids:
            count = options.get('byzantine_count', (len(names) - 1) // 3)
            ids = sorted(Random(options['seed']).sample(range(len(names)), count))
        return dict(mode=options['mode'], fault_model='local-proposal',
                    seed=options['seed'], delay_ms=options['delay_ms'],
                    delayed_launch_ids=ids, delayed_authorities=[names[i] for i in ids],
                    omitted_regular_leader_slots='every second, starting at slot 2')
    if 'crash_last' in options:
        ids = list(range(len(names) - options['crash_last'], len(names)))
    config = {name: value for name, value in options.items() if name not in ('byzantine_ids', 'crash_last')}
    config['byzantine_authorities'] = [names[i] for i in ids]
    if config.get('fault_model') == 'crash-stop':
        config['byzantine_ids'] = list(ids)
    return config


def active_launch_ids(nodes, adversary=None):
    """Keep original launch IDs; A1 omits primary, workers and clients together."""
    missing = set()
    if adversary and adversary.get('fault_model') == 'crash-stop':
        missing.update(adversary['byzantine_ids'])
    if any(type(i) is not int or i < 0 or i >= nodes for i in missing):
        raise ValueError('Inactive authority outside the committee')
    active = [i for i in range(nodes) if i not in missing]
    if not active:
        raise ValueError('At least one authority must be started')
    return active


def measurement_population(directory, primary_ids, adversary=None, committee_size=None):
    """Check the log population for a live run or a saved measurement."""
    directory = Path(directory)
    saved_path = directory / 'measurement-population.json'
    if adversary is None and saved_path.exists():
        saved = json.loads(saved_path.read_text(encoding='utf-8'))
        if saved.get('fault_model') not in ('crash-stop', 'local-proposal'):
            return None
        if saved.get('schema_version') != 2:
            raise ValueError('Unknown measurement population version')
        if committee_size is not None and committee_size != saved['committee_size']:
            raise ValueError('Committee size differs from saved population')
        committee_size = saved['committee_size']
        if sorted(saved['primary_ids']) != sorted(primary_ids):
            raise ValueError('Saved population does not match primary logs')
        model = saved['fault_model']
        mode = saved.get('mode')
        crashed = saved['crashed_primary_ids']
    elif adversary and adversary.get('fault_model') in ('crash-stop', 'local-proposal'):
        model = adversary['fault_model']
        mode = adversary['mode']
        crashed = adversary['byzantine_ids'] if model == 'crash-stop' else []
    else:
        return None
    if (type(committee_size) is not int or committee_size < 2 or not isinstance(crashed, list)
            or (model == 'crash-stop' and not crashed) or len(set(crashed)) != len(crashed)
            or any(type(i) is not int or i < 0 or i >= committee_size for i in crashed)):
        raise ValueError('Invalid crashed authority IDs or committee size')
    if model == 'local-proposal' and (crashed or mode not in ('A3', 'A5')):
        raise ValueError('A3 and A5 require the full committee to be active')
    expected = set(range(committee_size)) - set(crashed)
    if set(primary_ids) != expected or len(primary_ids) != len(expected):
        raise ValueError('Primary logs must contain exactly the active authorities')
    logged_workers = {}
    for kind in ('client', 'worker'):
        actual = {}
        for path in directory.glob(f'{kind}-*.log'):
            parts = path.stem.split('-')
            if len(parts) != 3:
                raise ValueError(f'Invalid {kind} log identity: {path.name}')
            ident, worker = map(int, parts[1:])
            actual.setdefault(ident, set()).add(worker)
        logged_workers[kind] = actual
        if set(actual) != expected or len({tuple(sorted(v)) for v in actual.values()}) != 1:
            raise ValueError(f'{kind} logs do not cover the active authorities')
    if logged_workers['client'] != logged_workers['worker']:
        raise ValueError('Client and worker log identities differ')
    population = dict(schema_version=2, fault_model=model, committee_size=committee_size,
                      primary_ids=primary_ids, excluded_primary_ids=[], crashed_primary_ids=sorted(crashed))
    if model == 'local-proposal':
        population['mode'] = mode
    return population


def write_run_snapshot(directory, benchmark_parameters, node_parameters,
                       launch_authorities, config, nodes, rate, run, *, protocol):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = Path(directory) / f'{stamp}-{protocol}-n{nodes}-r{rate}-run{run}.parameters.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    snapshot = dict(
        protocol=protocol, created_utc=stamp, nodes=nodes, rate=rate, run=run,
        benchmark_parameters=benchmark_parameters, node_parameters=node_parameters,
        launch_authorities=list(launch_authorities), adversary=config,
    )
    active = active_launch_ids(nodes, config)
    workers = int(benchmark_parameters['workers'])
    total_workers = nodes * workers
    rate_per_worker = (rate + total_workers - 1) // total_workers
    snapshot.update(fault_model=config.get('fault_model', 'none'),
                    active_launch_ids=active,
                    inactive_launch_ids=[i for i in range(nodes) if i not in active],
                    offered_rate_tps=rate_per_worker * len(active) * workers)
    output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return str(output)
