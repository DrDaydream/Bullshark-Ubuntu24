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
            .map(|id| (PublicKey([id as u8; 32]), member.clone()))
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

fn simulated_order(mode: &str) -> Vec<usize> {
    // All synthetic events in this fixture belong to the single observer 3.
    let fixture = r#"
import sys
from benchmark.adversary_sim import Event, simulate

mode, nodes, rounds = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
events = [Event((r - 1) * 4, r, sender, 3, sender in (0, 1), 64)
          for r in range(1, rounds + 1) for sender in range(nodes)]
for row in simulate(events, mode=mode, nodes=nodes, faulty_ids=[1],
                    delayed_receivers=[3], delay_ms=6):
    print(row.input_index)
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
    std::str::from_utf8(&output.stdout)
        .unwrap()
        .lines()
        .map(|s| s.parse().unwrap())
        .collect()
}

// The primary normally gives consensus each certificate after its entire history.
// Here virtual arrivals can cross round boundaries, so hold children until their
// parents arrive, preserving arrival order among ready siblings.
fn causally_ready(order: &[usize], dag: &[Certificate], committee: &Committee) -> Vec<usize> {
    let mut known: BTreeSet<_> = Certificate::genesis(committee)
        .iter()
        .map(Certificate::digest)
        .collect();
    let mut pending = Vec::new();
    let mut ready = Vec::new();
    for &index in order {
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
    ready
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

#[tokio::test]
#[ignore = "opt-in replay uses the Python benchmark model"]
async fn replay_synthetic_schedules() {
    let (committee, dag) = synthetic_dag();
    let baseline = commits(&(0..dag.len()).collect::<Vec<_>>(), &dag, &committee).await;
    // Four members commit rounds 1 through 3, followed by the round-4 leader.
    assert_eq!(baseline.len(), 3 * NODES + 1);
    let leader = dag
        .iter()
        .find(|c| c.round() == 4 && c.origin() == committee.leader(4))
        .unwrap();
    assert_eq!(baseline.last(), Some(&leader.digest()));

    let mut replayed = Vec::new();
    for mode in &["A3", "A5"] {
        let arrivals = simulated_order(mode);
        let ready = causally_ready(&arrivals, &dag, &committee);
        assert_ne!(ready, arrivals, "the fixture must exercise child buffering");
        assert_eq!(
            commits(&ready, &dag, &committee).await,
            baseline,
            "{}",
            mode
        );
        replayed.push(ready);
    }
    assert_ne!(
        replayed[0], replayed[1],
        "the two delivery orders must differ"
    );
}
