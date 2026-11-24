use config::{Authority, Committee, PrimaryAddresses};
use consensus::Consensus;
use crypto::{Digest, Hash as _, PublicKey};
use primary::{Certificate, Header};
use std::collections::BTreeSet;
use std::path::Path;
use std::process::Command;
use std::time::Duration;
use tokio::sync::mpsc::channel;
use tokio::time::timeout;

const NODES: usize = 4;
const ROUNDS: u64 = 5;

fn synthetic_dag() -> (Committee, Vec<Certificate>) {
    let unused = ([0, 0, 0, 0], 0).into();
    let member = Authority {
        stake: 1,
        primary: PrimaryAddresses {
            primary_to_primary: unused,
            worker_to_primary: unused,
        },
        workers: Default::default(),
    };
    let committee = Committee {
        authorities: (0..NODES)
            .map(|id| {
                let mut key = [0; 32];
                key[0] = id as u8;
                (PublicKey(key), member.clone())
            })
            .collect(),
    };
    let mut parents: BTreeSet<_> = Certificate::genesis(&committee)
        .iter()
        .map(Certificate::digest)
        .collect();
    let mut dag = Vec::new();
    for round in 1..=ROUNDS {
        let start = dag.len();
        for author in committee.authorities.keys() {
            let mut header = Header {
                author: *author,
                round,
                parents: parents.clone(),
                ..Header::default()
            };
            header.id = header.digest();
            dag.push(Certificate {
                header,
                ..Certificate::default()
            });
        }
        parents = dag[start..].iter().map(Certificate::digest).collect();
    }
    (committee, dag)
}

// Replay one labelled trace across the whole virtual committee so a sender's
// pending budget is shared across observers. The event index encodes both the
// certificate (index / NODES) and its destination (index % NODES).
fn simulated_orders(mode: &str, reference_delays: &[usize]) -> Vec<(Vec<usize>, usize)> {
    let fixture = r#"
import sys
from benchmark.adversary_sim import Event, simulate

mode = sys.argv[1]
nodes, rounds = map(int, sys.argv[2:])
# Synthetic certificates (round, origin) (2, 2) and (3, 3) arrive by relay.
relays = {(2, 2): 1, (3, 3): 0}
events = []
for r in range(1, rounds + 1):
    for origin in range(nodes):
        sender = relays.get((r, origin), origin)
        for observer in range(nodes):
            marked = origin == r % nodes and sender != observer
            events.append(Event((r - 1) * 4, r, sender, observer, marked, 64))
for row in simulate(events, mode=mode, nodes=nodes, faulty_ids=[1], seed=1, delay_ms=6):
    print(row.input_index, row.action)
"#;
    let interpreter = if cfg!(windows) { "python" } else { "python3" };
    let output = Command::new(interpreter)
        .current_dir(Path::new(env!("CARGO_MANIFEST_DIR")).join("../benchmark"))
        .args(&[
            "-B",
            "-c",
            fixture,
            mode,
            &NODES.to_string(),
            &ROUNDS.to_string(),
        ])
        .output()
        .expect("the replay uses python3 with the standard library");
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let mut orders = vec![(Vec::new(), 0); NODES];
    let mut delayed = Vec::new();
    for line in std::str::from_utf8(&output.stdout).unwrap().lines() {
        let (index, action) = line.split_once(' ').unwrap();
        let index: usize = index.parse().unwrap();
        let (order, count) = &mut orders[index % NODES];
        order.push(index / NODES);
        if action == "delayed" {
            *count += 1;
            delayed.push(index);
        } else {
            assert_eq!(action, "direct");
        }
    }
    delayed.sort_unstable();
    assert_eq!(
        delayed, reference_delays,
        "{} differs from the Orca fixture",
        mode
    );
    orders
}

// The primary normally gives consensus each certificate after its entire history.
// Here virtual arrivals can cross round boundaries, so hold children until their
// parents arrive, preserving arrival order among ready siblings.
fn causally_ready(
    order: &[usize],
    dag: &[Certificate],
    committee: &Committee,
) -> (Vec<usize>, usize) {
    let mut known: BTreeSet<_> = Certificate::genesis(committee)
        .iter()
        .map(Certificate::digest)
        .collect();
    let mut pending = Vec::new();
    let mut ready = Vec::new();
    let mut buffered = 0;
    for &index in order {
        if !dag[index].header.parents.is_subset(&known) {
            buffered += 1;
        }
        pending.push(index);
        while let Some(i) = pending
            .iter()
            .position(|&id| dag[id].header.parents.is_subset(&known))
        {
            let id = pending.remove(i);
            assert!(known.insert(dag[id].digest()), "duplicate certificate");
            ready.push(id);
        }
    }
    assert!(pending.is_empty(), "unresolved synthetic parents");
    assert_eq!(ready.len(), dag.len());
    (ready, buffered)
}

async fn commits(order: &[usize], dag: &[Certificate], committee: &Committee) -> Vec<Digest> {
    let (input, rx_input) = channel(dag.len());
    let (primary, _rx_primary) = channel(dag.len());
    let (output, mut rx_output) = channel(dag.len());
    Consensus::spawn(committee.clone(), 50, rx_input, primary, output);
    timeout(Duration::from_secs(5), async {
        for &index in order {
            input.send(dag[index].clone()).await.unwrap();
        }
        drop(input);
        let mut committed = Vec::new();
        while let Some(cert) = rx_output.recv().await {
            committed.push(cert.digest());
        }
        committed
    })
    .await
    .expect("in-memory consensus did not finish")
}

pub async fn replay() {
    let (committee, dag) = synthetic_dag();
    let direct: Vec<_> = (0..dag.len()).collect();
    let baseline = commits(&direct, &dag, &committee).await;
    // Four members commit rounds 1 through 3, followed by the round-4 leader.
    assert_eq!(baseline.len(), 3 * NODES + 1);
    let leader = dag
        .iter()
        .find(|c| c.round() == 4 && c.origin() == committee.leader(4))
        .unwrap();
    assert_eq!(baseline.last(), Some(&leader.digest()));

    println!("mode observer delivered delayed parent_buffered committed");
    // Zero-based indexes emitted by both Orca reference policies for this trace.
    let replays = [
        ("A0", vec![(direct.clone(), 0); NODES]),
        ("A3", simulated_orders("A3", &[4, 7, 24, 27, 68, 71])),
        (
            "A5",
            simulated_orders("A5", &[4, 7, 24, 27, 46, 50, 51, 68, 71]),
        ),
    ];
    for (mode, orders) in replays.iter() {
        for (observer, (arrivals, delayed)) in orders.iter().enumerate() {
            let (ready, buffered) = causally_ready(arrivals, &dag, &committee);
            if *delayed > 0 {
                assert!(buffered > 0);
                assert_ne!(&ready, arrivals, "children must wait for parents");
            } else {
                assert_eq!(buffered, 0);
                assert_eq!(arrivals, &direct);
                assert_eq!(ready, direct);
            }
            let committed = commits(&ready, &dag, &committee).await;
            assert_eq!(committed, baseline, "{} observer {}", mode, observer);
            println!(
                "{} {} {} {} {} {}",
                mode,
                observer,
                arrivals.len(),
                delayed,
                buffered,
                committed.len()
            );
        }
    }
}
