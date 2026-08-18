#!/usr/bin/env bash
set -Eeuo pipefail

NODES="${1:-}"
DURATION="${2:-20}"
TOTAL_RATE="${3:-10000}"
FAULTS="${BULLSHARK_FAULTS:-0}"
ADVERSARY_SEED="${BULLSHARK_ADVERSARY_SEED:-0}"
CLIENT_DURING_SILENCE="${BULLSHARK_CLIENT_DURING_SILENCE:-pause}"
case "$NODES" in 10|20|50) ;; *) echo "Usage: $0 <10|20|50> [seconds] [total-tps]" >&2; exit 2;; esac
[[ "$DURATION" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ "$TOTAL_RATE" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ "$FAULTS" =~ ^[0-9]+$ ]] && (( FAULTS < NODES )) || { echo "BULLSHARK_FAULTS must be smaller than the node count" >&2; exit 2; }
[[ "$ADVERSARY_SEED" =~ ^[0-9]+$ ]] || { echo "BULLSHARK_ADVERSARY_SEED must be a non-negative integer" >&2; exit 2; }
case "$CLIENT_DURING_SILENCE" in send|pause) ;; *) echo "BULLSHARK_CLIENT_DURING_SILENCE must be send or pause" >&2; exit 2;; esac

REMOTE_USER="${REMOTE_USER:-ubuntu}"
REMOTE_DIR="${REMOTE_DIR:-/home/ubuntu/Bullshark-Ubuntu24}"
HOSTS_FILE="${HOSTS_FILE:-deploy/hosts-${NODES}.txt}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/bullshark-aws.pem}"
MAX_PARALLEL="${MAX_PARALLEL:-10}"
READY_TIMEOUT="${READY_TIMEOUT:-240}"
TX_SIZE="${TX_SIZE:-512}"
LOCAL_LOGS="benchmark/logs"
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)

[[ -f "$HOSTS_FILE" ]] || { echo "Missing $HOSTS_FILE" >&2; exit 1; }
[[ -f "$SSH_KEY" ]] || { echo "Missing SSH key $SSH_KEY" >&2; exit 1; }
mapfile -t IPS < <(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$HOSTS_FILE" | awk 'NF')
[[ "${#IPS[@]}" -eq "$NODES" ]] || { echo "$HOSTS_FILE must contain $NODES IPs" >&2; exit 1; }
[[ "$(printf '%s\n' "${IPS[@]}" | sort -u | wc -l)" -eq "$NODES" ]] || { echo "Duplicate IP" >&2; exit 1; }

RATE_SHARE=$(((TOTAL_RATE + NODES - 1) / NODES))
TX_NODES=""
for ip in "${IPS[@]}"; do TX_NODES+="${ip}:3003 "; done
[[ -f deploy/committee.json && -f deploy/parameters.json ]] || { echo "Missing deploy/committee.json or deploy/parameters.json" >&2; exit 1; }
KEY_FILES=()
for ((i=0; i<NODES; i++)); do
  [[ -f "deploy/node-${i}.json" ]] || { echo "Missing deploy/node-${i}.json" >&2; exit 1; }
  KEY_FILES+=("deploy/node-${i}.json")
done
CLIENT_SILENCE_SLOT_MS="${BULLSHARK_CLIENT_SILENCE_SLOT_MS:-}"
if [[ -z "$CLIENT_SILENCE_SLOT_MS" ]]; then
  CLIENT_SILENCE_SLOT_MS="$(python3 -c 'import json; print(json.load(open("deploy/parameters.json"))["max_header_delay"])')"
fi
[[ "$CLIENT_SILENCE_SLOT_MS" =~ ^[1-9][0-9]*$ ]] || { echo "BULLSHARK_CLIENT_SILENCE_SLOT_MS must be positive" >&2; exit 2; }
mapfile -t CLIENT_SCHEDULES < <(
  BULLSHARK_CLIENT_DURING_SILENCE="$CLIENT_DURING_SILENCE" \
  BULLSHARK_ADVERSARY_SEED="$ADVERSARY_SEED" \
  PYTHONPATH=benchmark python3 -m benchmark.adversary_schedule \
    --committee deploy/committee.json --faults "$FAULTS" \
    --duration "$DURATION" --slot-ms "$CLIENT_SILENCE_SLOT_MS" \
    --key-files "${KEY_FILES[@]}"
)
[[ "${#CLIENT_SCHEDULES[@]}" -eq "$NODES" ]] || { echo "Failed to generate client silence schedules" >&2; exit 1; }
remote() { ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@$1" "$2"; }
wait_batch() { (( $1 % MAX_PARALLEL == 0 )) && wait || true; }
stop_all() {
  local count=0
  for ip in "${IPS[@]}"; do
    remote "$ip" "tmux kill-session -t bull-client 2>/dev/null || true; tmux kill-session -t bull-primary 2>/dev/null || true; tmux kill-session -t bull-worker 2>/dev/null || true" &
    count=$((count+1)); wait_batch "$count"
  done
  wait || true
}
trap stop_all EXIT INT TERM

echo "nodes=$NODES duration=${DURATION}s total-rate=$TOTAL_RATE per-client=$RATE_SHARE faults=$FAULTS seed=$ADVERSARY_SEED client-during-silence=$CLIENT_DURING_SILENCE"
for i in "${!IPS[@]}"; do
  remote "${IPS[$i]}" "test -x '$REMOTE_DIR/target/release/node' && test -x '$REMOTE_DIR/target/release/benchmark_client' && test -f '$REMOTE_DIR/deploy/node-${i}.json'"
done

count=0
for ip in "${IPS[@]}"; do
  remote "$ip" "tmux kill-session -t bull-client 2>/dev/null || true; tmux kill-session -t bull-primary 2>/dev/null || true; tmux kill-session -t bull-worker 2>/dev/null || true; cd '$REMOTE_DIR'; rm -rf run/db-primary run/db-worker run/logs; mkdir -p run/logs" &
  count=$((count+1)); wait_batch "$count"
done
wait

for i in "${!IPS[@]}"; do
  remote "${IPS[$i]}" "cd '$REMOTE_DIR' && tmux new-session -d -s bull-worker \"RUST_LOG=info ./target/release/node -vv run --keys deploy/node-${i}.json --committee deploy/committee.json --parameters deploy/parameters.json --store run/db-worker worker --id 0 |& tee run/logs/worker-${i}-0.log\""
done
for i in "${!IPS[@]}"; do
  remote "${IPS[$i]}" "cd '$REMOTE_DIR' && tmux new-session -d -s bull-primary \"RUST_LOG=info BULLSHARK_FAULTS='$FAULTS' BULLSHARK_ADVERSARY_SEED='$ADVERSARY_SEED' BULLSHARK_CLIENT_DURING_SILENCE='$CLIENT_DURING_SILENCE' ./target/release/node -vv run --keys deploy/node-${i}.json --committee deploy/committee.json --parameters deploy/parameters.json --store run/db-primary primary |& tee run/logs/primary-${i}.log\""
done

for ((elapsed=0; elapsed<READY_TIMEOUT; elapsed+=3)); do
  ready=0
  for i in "${!IPS[@]}"; do
    remote "${IPS[$i]}" "ss -ltn | grep -q ':3003 '" && ready=$((ready+1)) || true
  done
  (( ready == NODES )) && break
  sleep 3
done
(( ready == NODES )) || { echo "Only $ready/$NODES workers listen on port 3003" >&2; exit 1; }

for i in "${!IPS[@]}"; do
  SILENCE_ARGS=""
  if [[ "$CLIENT_DURING_SILENCE" == "pause" ]]; then
    SILENCE_ARGS="--silence-schedule '${CLIENT_SCHEDULES[$i]}' --silence-slot-ms '$CLIENT_SILENCE_SLOT_MS'"
  fi
  remote "${IPS[$i]}" "cd '$REMOTE_DIR' && tmux new-session -d -s bull-client \"RUST_LOG=info ./target/release/benchmark_client '${IPS[$i]}:3003' --size '$TX_SIZE' --rate '$RATE_SHARE' $SILENCE_ARGS --nodes $TX_NODES |& tee run/logs/client-${i}-0.log\""
done

for ((elapsed=0; elapsed<READY_TIMEOUT; elapsed+=3)); do
  ready=0
  for i in "${!IPS[@]}"; do
    remote "${IPS[$i]}" "grep -q 'Start sending transactions' '$REMOTE_DIR/run/logs/client-${i}-0.log'" && ready=$((ready+1)) || true
  done
  echo "clients ready=$ready/$NODES"
  (( ready == NODES )) && break
  sleep 3
done
(( ready == NODES )) || { echo "Clients did not become ready" >&2; exit 1; }

sleep "$DURATION"
stop_all
trap - EXIT INT TERM
rm -rf "$LOCAL_LOGS"
mkdir -p "$LOCAL_LOGS"
for i in "${!IPS[@]}"; do
  scp "${SSH_OPTS[@]}" "${REMOTE_USER}@${IPS[$i]}:$REMOTE_DIR/run/logs/primary-${i}.log" "$LOCAL_LOGS/"
  scp "${SSH_OPTS[@]}" "${REMOTE_USER}@${IPS[$i]}:$REMOTE_DIR/run/logs/worker-${i}-0.log" "$LOCAL_LOGS/"
  scp "${SSH_OPTS[@]}" "${REMOTE_USER}@${IPS[$i]}:$REMOTE_DIR/run/logs/client-${i}-0.log" "$LOCAL_LOGS/"
done
cd benchmark
BULLSHARK_FAULTS="$FAULTS" python3 - <<'PY'
import os
from benchmark.logs import LogParser
print(LogParser.process("logs", faults=int(os.environ["BULLSHARK_FAULTS"])).result())
PY
