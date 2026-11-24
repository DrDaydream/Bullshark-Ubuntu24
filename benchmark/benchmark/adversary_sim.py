"""Replay pre-labelled synthetic events using integer virtual time."""

from collections import Counter, deque
from dataclasses import dataclass
from hashlib import sha512


@dataclass(frozen=True)
class Event:
    time_ms: int
    round: int
    sender: int
    receiver: int
    marked: bool
    size_bytes: int


@dataclass(frozen=True)
class Delivery:
    input_index: int
    time_ms: int
    action: str


def _integer(name, value, minimum):
    if type(value) is not int or value < minimum:
        raise ValueError(f'{name} must be an integer >= {minimum}')


def _node_id(value, nodes):
    _integer('virtual node ID', value, 0)
    if value >= nodes:
        raise ValueError(f'virtual node ID {value} must be below {nodes}')
    return value


def _recipient_group(nodes, faulty_ids, round_number, seed):
    """Rank synthetic identities in the same seed/round domain as the Orca fixture."""
    prefix = (b'narwhal-dynamic-adversary-v1' + seed.to_bytes(8, 'little')
              + round_number.to_bytes(8, 'little') + b'\x03')
    honest = [i for i in range(nodes) if i not in faulty_ids]
    ranked = sorted(honest, key=lambda i: sha512(prefix + i.to_bytes(32, 'little')).digest()[:32])
    return set(ranked[:(len(ranked) + 1) // 2])


def simulate(events, *, mode, nodes, faulty_ids, delayed_receivers=None, seed=1, start_round=1,
             rounds=0, delay_ms=200, max_pending_messages=4096,
             max_pending_bytes=67108864):
    """Return deliveries in virtual time order for a synthetic A3 or A5 trace.

    Only events explicitly marked in the input and addressed to one of the
    selected virtual receivers qualify. A fixed list overrides the default
    seeded half of the honest receiver IDs for each round. A3 also requires the sender to be in
    faulty_ids. A positive rounds value limits the window to that many rounds
    starting at start_round. Zero extends the window through the trace.

    Each virtual sender has its own pending byte and event limits. Reaching a
    limit delivers the new event immediately with action 'capacity'. Existing
    events due at the same virtual time are delivered first, in arrival order.
    """
    if mode not in ('A3', 'A5'):
        raise ValueError('mode must be A3 or A5')
    _integer('nodes', nodes, 1)
    _integer('start_round', start_round, 1)
    _integer('rounds', rounds, 0)
    _integer('delay_ms', delay_ms, 1)
    _integer('max_pending_messages', max_pending_messages, 1)
    _integer('max_pending_bytes', max_pending_bytes, 1)
    _integer('seed', seed, 0)
    if seed >= 2**64:
        raise ValueError('seed must fit in 64 bits')
    if start_round + rounds >= 2**64:
        raise ValueError('round window must fit in 64 bits')
    if delay_ms > 86_400_000:
        raise ValueError('delay_ms must be at most 86400000')
    selected = [_node_id(node, nodes) for node in faulty_ids]
    faulty_ids = set(selected)
    if len(faulty_ids) != len(selected):
        raise ValueError('faulty_ids must be distinct')
    if len(faulty_ids) > (nodes - 1) // 3 or (mode == 'A3' and not faulty_ids):
        raise ValueError('Supply a valid fault set for this committee and mode')
    if delayed_receivers is not None:
        delayed_receivers = {_node_id(node, nodes) for node in delayed_receivers}
        if faulty_ids & delayed_receivers:
            raise ValueError('Delayed receivers must be honest virtual nodes')
    group_round = None
    group = delayed_receivers

    pending = deque()
    pending_counts = Counter()
    pending_bytes = Counter()
    delivered = []

    def release_first():
        delivery, event = pending.popleft()
        pending_counts[event.sender] -= 1
        pending_bytes[event.sender] -= event.size_bytes
        delivered.append(delivery)

    previous_time = 0
    for index, event in enumerate(events):
        _integer('time_ms', event.time_ms, previous_time)
        previous_time = event.time_ms
        _integer('round', event.round, 1)
        if event.round >= 2**64:
            raise ValueError('round must fit in 64 bits')
        _integer('size_bytes', event.size_bytes, 0)
        _node_id(event.sender, nodes)
        _node_id(event.receiver, nodes)
        if type(event.marked) is not bool:
            raise ValueError('marked must be a boolean')

        while pending and pending[0][0].time_ms <= event.time_ms:
            release_first()
        in_window = event.round >= start_round and (rounds == 0 or event.round < start_round + rounds)
        eligible = event.marked and in_window and (mode == 'A5' or event.sender in faulty_ids)
        if eligible and delayed_receivers is None and group_round != event.round:
            group = _recipient_group(nodes, faulty_ids, event.round, seed)
            group_round = event.round
        if not eligible or event.receiver not in group:
            delivered.append(Delivery(index, event.time_ms, 'direct'))
        elif (pending_counts[event.sender] >= max_pending_messages
              or pending_bytes[event.sender] + event.size_bytes > max_pending_bytes):
            delivered.append(Delivery(index, event.time_ms, 'capacity'))
        else:
            pending_counts[event.sender] += 1
            pending_bytes[event.sender] += event.size_bytes
            due = Delivery(index, event.time_ms + delay_ms, 'delayed')
            pending.append((due, event))

    while pending:
        release_first()
    return delivered
