# Bullshark-Ubuntu24

[![Rust](https://github.com/DrDaydream/Bullshark-Ubuntu24/actions/workflows/rust.yml/badge.svg)](https://github.com/DrDaydream/Bullshark-Ubuntu24/actions/workflows/rust.yml)
[![Ubuntu](https://img.shields.io/badge/Ubuntu-24.04-E95420?style=flat-square&logo=ubuntu)](https://ubuntu.com/)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue.svg?style=flat-square)](LICENSE)

This repository provides an Ubuntu 24.04-compatible research implementation of [Bullshark](https://arxiv.org/pdf/2201.05677.pdf), built on the Narwhal DAG mempool. It also includes deterministic active-adversary scheduling, fallback commit/skip accounting, leader and non-leader latency statistics, and local/AWS benchmark tooling.

The codebase is intended for research and benchmarking rather than production use. It uses real cryptography ([dalek](https://doc.dalek.rs/ed25519_dalek)), asynchronous networking ([Tokio](https://docs.rs/tokio)), and persistent storage ([RocksDB](https://rocksdb.org/)).

## Quick Start

The protocol is implemented in Rust. Python benchmark scripts use [Fabric](https://www.fabfile.org/) to build the binaries, start local processes, collect logs, and print results.

Install the Ubuntu 24.04 dependencies directly into the current user environment:

~~~bash
git clone https://github.com/DrDaydream/Bullshark-Ubuntu24.git
cd Bullshark-Ubuntu24

sudo apt-get update
sudo apt-get install -y \
  build-essential cmake clang-14 libclang-14-dev curl git tmux \
  python3 python3-pip

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"

python3 -m pip install --user --break-system-packages \
  -r benchmark/requirements.txt
export PATH="$HOME/.local/bin:$PATH"
~~~

The local benchmark automatically detects installed LLVM versions. The following explicit environment remains useful when diagnosing RocksDB bindgen failures:

~~~bash
export LIBCLANG_PATH=/usr/lib/llvm-14/lib
export CLANG_PATH=/usr/bin/clang-14
export CC=/usr/bin/clang-14
export CXX=/usr/bin/clang++-14
export CXXFLAGS='-include cstdint'
~~~

Edit `benchmark/fabfile.py` to configure the local run:

~~~python
bench_params = {
    'faults': 0,
    'nodes': 4,
    'workers': 1,
    'rate': 50_000,
    'tx_size': 512,
    'duration': 20,
}
~~~

The dynamic adversary keeps all processes online, so `faults` is a scheduling parameter rather than a count of processes to omit. Keep `nodes >= 3 * faults + 1`.

Run:

~~~bash
cd benchmark
fab local
~~~

The first execution compiles the workspace in release mode with the `benchmark` feature and may take several minutes.

### Local adversary options

Set `'faults': 0` in `benchmark/fabfile.py` for the baseline. With a positive `faults` value, use:

~~~bash
# Default adversarial workload: deterministic schedule and paused client
# traffic during the selected authority's silent slots.
BULLSHARK_ADVERSARY_SEED=42 \
BULLSHARK_CLIENT_DURING_SILENCE=pause \
fab local

# Keep client and batch input while the selected Primary remains silent.
BULLSHARK_ADVERSARY_SEED=42 \
BULLSHARK_CLIENT_DURING_SILENCE=send \
fab local

# Override the wall-clock slot used by the pre-generated client schedule.
BULLSHARK_ADVERSARY_SEED=42 \
BULLSHARK_CLIENT_DURING_SILENCE=pause \
BULLSHARK_CLIENT_SILENCE_SLOT_MS=200 \
fab local
~~~

| Variable | Default | Meaning |
|---|---|---|
| `BULLSHARK_ADVERSARY_SEED` | `0` | Deterministic per-round schedule seed |
| `BULLSHARK_CLIENT_DURING_SILENCE` | `pause` | Pause or preserve client input |
| `BULLSHARK_CLIENT_SILENCE_SLOT_MS` | `max_header_delay` | Client schedule slot in milliseconds |

With `faults > 0`, every round selects exactly f adversarial authorities. The round's steady leader is always selected and is therefore unavailable; the remaining f-1 authorities are selected deterministically from the seed. Silent Primaries suppress Header creation but continue receiving protocol messages.

When a steady leader is unavailable, Bullshark selects a common fallback leader. A fallback requires `2f+1` causal support to commit; otherwise it is counted as skipped. The final report separates steady-state leaders, fallback leaders, fallback commits, and fallback skips.

### No-adversary baseline (`faults = 0`)

Set `'faults': 0` in `benchmark/fabfile.py` and run:

~~~bash
RUST_LOG=info fab local
~~~

The following output was produced by a 4-node, 50,000 tx/s, 20-second local run:

~~~text
-----------------------------------------
 SUMMARY:
-----------------------------------------
 + CONFIG:
 Faults: 0 node(s)
 Committee size: 4 node(s)
 Worker(s) per node: 1 worker(s)
 Collocate primary and workers: True
 Input rate: 50,000 tx/s
 Transaction size: 512 B
 Execution time: 20 s

 Header size: 1,000 B
 Max header delay: 200 ms
 GC depth: 50 round(s)
 Sync retry delay: 10,000 ms
 Sync retry nodes: 3 node(s)
 batch size: 500,000 B
 Max batch delay: 200 ms

 + RESULTS:
 Consensus TPS: 49,561 tx/s
 Consensus BPS: 25,375,178 B/s
 Consensus latency: 455 ms

 End-to-end TPS: 49,236 tx/s
 End-to-end BPS: 25,208,664 B/s
 End-to-end latency: 598 ms
 Leader commit latency: 203 ms
 Non-leader commit latency: 484 ms
 All committed headers latency: 452 ms
 Leader commit interval: 445 ms
 Non-leader rule-order latency: 484 ms
 Steady-state leader ratio: 78.57%
 Fallback leader ratio: 21.43%
 Fallback commit ratio: 0.00%
 Fallback skip ratio: 0.00%
-----------------------------------------
~~~

### Preserved adversarial result (`faults = 1`)

For comparison, this is the previously recorded 4-node, 1-fault, 20-second local result using the adversary commands above:

~~~text
-----------------------------------------
 SUMMARY:
-----------------------------------------
 + CONFIG:
 Faults: 1 node(s)
 Committee size: 4 node(s)
 Worker(s) per node: 1 worker(s)
 Collocate primary and workers: True
 Input rate: 50,000 tx/s
 Transaction size: 512 B
 Execution time: 20 s

 Header size: 1,000 B
 Max header delay: 200 ms
 GC depth: 50 round(s)
 Sync retry delay: 10,000 ms
 Sync retry nodes: 3 node(s)
 batch size: 500,000 B
 Max batch delay: 200 ms

 + RESULTS:
 Consensus TPS: 36,201 tx/s
 Consensus BPS: 18,534,964 B/s
 Consensus latency: 1,021 ms

 End-to-end TPS: 35,992 tx/s
 End-to-end BPS: 18,427,858 B/s
 End-to-end latency: 1,228 ms
 Leader commit latency: 630 ms
 Non-leader commit latency: 1,054 ms
 All committed headers latency: 1,018 ms
 Leader commit interval: 730 ms
 Non-leader rule-order latency: 1,054 ms
 Steady-state leader ratio: 0.00%
 Fallback leader ratio: 100.00%
 Fallback commit ratio: 100.00%
 Fallback skip ratio: 0.00%
-----------------------------------------
~~~

Results vary by machine and workload. `Consensus latency` covers header creation to consensus commit, while `End-to-end latency` starts when a sampled transaction is submitted by the client.


## License

This software is licensed under [Apache License 2.0](LICENSE).
