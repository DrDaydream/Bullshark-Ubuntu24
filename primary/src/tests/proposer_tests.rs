// Copyright(C) Facebook, Inc. and its affiliates.
use super::*;
use crate::common::{committee, keys};
use tokio::sync::mpsc::channel;

#[tokio::test]
async fn propose_empty() {
    let (name, secret) = keys().pop().unwrap();
    let signature_service = SignatureService::new(secret);

    let (_tx_parents, rx_parents) = channel(1);
    let (_tx_our_digests, rx_our_digests) = channel(1);
    let (tx_headers, mut rx_headers) = channel(1);

    // Spawn the proposer.
    Proposer::spawn(
        name,
        committee(),
        signature_service,
        /* header_size */ 1_000,
        /* max_header_delay */ 20,
        /* rx_core */ rx_parents,
        /* rx_workers */ rx_our_digests,
        /* tx_core */ tx_headers,
    );

    // Ensure the proposer makes a correct empty header.
    let header = rx_headers.recv().await.unwrap();
    assert_eq!(header.round, 1);
    assert!(header.payload.is_empty());
    assert!(header.verify(&committee()).is_ok());
}

#[tokio::test]
async fn propose_payload() {
    let (name, secret) = keys().pop().unwrap();
    let signature_service = SignatureService::new(secret);

    let (_tx_parents, rx_parents) = channel(1);
    let (tx_our_digests, rx_our_digests) = channel(1);
    let (tx_headers, mut rx_headers) = channel(1);

    // Spawn the proposer.
    Proposer::spawn(
        name,
        committee(),
        signature_service,
        /* header_size */ 32,
        /* max_header_delay */ 1_000_000, // Ensure it is not triggered.
        /* rx_core */ rx_parents,
        /* rx_workers */ rx_our_digests,
        /* tx_core */ tx_headers,
    );

    // Send enough digests for the header payload.
    let digest = Digest(name.0);
    let worker_id = 0;
    tx_our_digests
        .send((digest.clone(), worker_id))
        .await
        .unwrap();

    // Ensure the proposer makes a correct header from the provided payload.
    let header = rx_headers.recv().await.unwrap();
    assert_eq!(header.round, 1);
    assert_eq!(header.payload.get(&digest), Some(&worker_id));
    assert!(header.verify(&committee()).is_ok());
}

#[cfg(feature = "benchmark")]
#[tokio::test]
async fn omitted_leader_keeps_pending_batch_for_round_five() {
    use futures::FutureExt;

    for (mode, delayed) in &[("A3", false), ("A5", true)] {
        let committee = committee();
        let name = committee.leader(4);
        let (_, secret) = keys().into_iter().find(|(id, _)| *id == name).unwrap();
        let authorities: Vec<_> = committee.authorities.keys().copied().collect();
        let launch_id = authorities.iter().position(|key| *key == name).unwrap();
        let profile = serde_json::json!({
            "mode": mode, "launch_id": launch_id, "seed": 1, "delay_ms": 0, "delayed": delayed,
        });
        let profile = ProposalProfile::parse(&profile.to_string(), committee.size(), 0).unwrap();
        // The Core supplies quorum parents from the other three authorities in both rounds.
        let parents = |round| {
            authorities
                .iter()
                .copied()
                .filter(|id| *id != name)
                .map(|author| Certificate {
                    header: Header {
                        author,
                        round,
                        ..Header::default()
                    },
                    ..Certificate::default()
                })
                .collect::<Vec<_>>()
        };
        let round_three = parents(3);
        let round_four = parents(4);
        let expected_parents: std::collections::BTreeSet<_> =
            round_four.iter().map(|cert| cert.digest()).collect();
        let digest = Digest(name.0);
        let (tx_parents, rx_core) = channel(1);
        let (_tx_digests, rx_workers) = channel(1);
        let (tx_core, mut rx_headers) = channel(1);
        let mut proposer = Proposer {
            name,
            committee,
            authorities,
            adversary_faults: 0,
            adversary_seed: 0,
            proposal_profile: Some(profile),
            silence_round: 0,
            silent: false,
            pause_batches_during_silence: false,
            worker_addresses: Vec::new(),
            worker_network: ReliableSender::new(),
            worker_handlers: Vec::new(),
            signature_service: SignatureService::new(secret),
            header_size: digest.size(),
            max_header_delay: 10,
            rx_core,
            rx_workers,
            tx_core,
            round: 3,
            last_parents: round_three,
            last_leader: None,
            digests: vec![(digest.clone(), 0)],
            payload_size: digest.size(),
        };

        // Poll the real loop through the eligible slot before round-four parents arrive.
        let run = proposer.run();
        tokio::pin!(run);
        assert!(run.as_mut().now_or_never().is_none());
        assert!(
            rx_headers.try_recv().is_err(),
            "{} emitted a round-four header",
            mode
        );
        tx_parents.send((round_four, 4)).await.unwrap();
        let received = tokio::select! {
            _ = &mut run => panic!("proposer exited"),
            value = tokio::time::timeout(Duration::from_secs(2), rx_headers.recv()) => value,
        };
        let header = received.expect("round five timed out").unwrap();
        assert_eq!(header.round, 5, "{}", mode);
        assert_eq!(header.payload.get(&digest), Some(&0), "{}", mode);
        assert_eq!(header.parents, expected_parents, "{}", mode);
        assert!(header.verify(&crate::common::committee()).is_ok());
    }
}
