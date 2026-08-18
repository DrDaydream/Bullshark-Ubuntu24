use crypto::PublicKey;
use ed25519_dalek::{Digest as _, Sha512};
use std::convert::TryInto as _;

fn score(authority: &PublicKey, round: u64, seed: u64) -> [u8; 32] {
    let mut hasher = Sha512::new();
    hasher.update(b"narwhal-dynamic-adversary-v1");
    hasher.update(seed.to_le_bytes());
    hasher.update(round.to_le_bytes());
    hasher.update([0]);
    hasher.update(authority.as_ref());
    let output = hasher.finalize();
    output[..32].try_into().unwrap()
}

/// Test whether an authority belongs to this round's deterministic random
/// adversary set. The steady leader occupies one of the configured slots.
pub(crate) fn selected(
    authority: &PublicKey,
    authorities: &[PublicKey],
    round: u64,
    faults: usize,
    seed: u64,
    steady: PublicKey,
) -> bool {
    let faults = faults.min(authorities.len());
    if faults == 0 {
        return false;
    }
    if *authority == steady {
        return true;
    }

    let random_slots = faults.saturating_sub(1);
    if random_slots == 0 {
        return false;
    }

    let own_key = (score(authority, round, seed), *authority);
    let rank = authorities
        .iter()
        .filter(|candidate| **candidate != steady)
        .filter(|candidate| (score(candidate, round, seed), **candidate) < own_key)
        .count();
    rank < random_slots
}

#[cfg(test)]
mod tests {
    use super::*;

    fn authorities(count: u8) -> Vec<PublicKey> {
        (0..count).map(|value| PublicKey([value; 32])).collect()
    }

    #[test]
    fn steady_is_always_silent_and_fault_count_is_preserved() {
        let authorities = authorities(10);
        for round in 1..20 {
            let steady = authorities[round as usize % authorities.len()];
            assert!(selected(&steady, &authorities, round, 3, 7, steady));
            let count = authorities
                .iter()
                .filter(|authority| selected(authority, &authorities, round, 3, 7, steady))
                .count();
            assert_eq!(count, 3);
        }
    }

    #[test]
    fn one_fault_selects_only_the_steady_leader() {
        let authorities = authorities(4);
        let steady = authorities[2];
        let selected: Vec<_> = authorities
            .iter()
            .filter(|authority| selected(authority, &authorities, 9, 1, 3, steady))
            .copied()
            .collect();
        assert_eq!(selected, vec![steady]);
    }

    #[test]
    fn schedule_is_reproducible_and_changes_between_rounds() {
        let authorities = authorities(10);
        let schedule = |seed| {
            (1..=20)
                .map(|round| {
                    let steady = authorities[round as usize % authorities.len()];
                    authorities
                        .iter()
                        .filter(|authority| {
                            selected(authority, &authorities, round, 3, seed, steady)
                        })
                        .copied()
                        .collect::<Vec<_>>()
                })
                .collect::<Vec<_>>()
        };

        let first = schedule(11);
        assert_eq!(first, schedule(11));
        assert!(first.windows(2).any(|rounds| rounds[0] != rounds[1]));
        assert_ne!(first, schedule(12));
    }
}
