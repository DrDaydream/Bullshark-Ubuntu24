use ed25519_dalek::{Digest as _, Sha512};
use serde::Deserialize;
use std::{convert::TryInto, env};

#[derive(Debug, Deserialize, PartialEq)]
pub(super) enum Mode {
    A3,
    A5,
}

/// Per-process settings from the benchmark launcher. IDs refer to launch order,
/// so the timing samples do not depend on freshly generated signing keys.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ProposalProfile {
    pub mode: Mode,
    pub launch_id: u64,
    seed: u64,
    delay_ms: u64,
    delayed: bool,
}

#[derive(Debug, PartialEq)]
pub(super) enum Action {
    Propose,
    Wait(u64),
    Omit,
}

impl ProposalProfile {
    pub fn from_env(committee_size: usize, legacy_faults: usize) -> Option<Self> {
        let value = match env::var("BULLSHARK_BENCH_PROPOSAL_PROFILE") {
            Ok(value) if value.is_empty() => return None,
            Ok(value) => value,
            Err(env::VarError::NotPresent) => return None,
            Err(error) => panic!("Invalid benchmark proposal profile: {}", error),
        };
        let profile = Self::parse(&value, committee_size, legacy_faults)
            .unwrap_or_else(|message| panic!("Invalid benchmark proposal profile: {}", message));
        Some(profile)
    }

    pub fn parse(value: &str, committee_size: usize, legacy_faults: usize) -> Result<Self, String> {
        let profile: Self = serde_json::from_str(value).map_err(|err| err.to_string())?;
        if legacy_faults != 0 {
            return Err("A3/A5 require BULLSHARK_FAULTS=0".into());
        }
        if profile.launch_id >= committee_size as u64 {
            return Err("launch_id must be in the committee".into());
        }
        if profile.mode == Mode::A5 && !profile.delayed {
            return Err("A5 requires every proposer to participate".into());
        }
        if profile.delay_ms > 86_400_000 {
            return Err("delay_ms exceeds one day".into());
        }
        Ok(profile)
    }

    pub fn action(&self, round: u64, is_local_leader: impl FnOnce() -> bool) -> Action {
        // Regular leaders occupy rounds 2, 4, 6, ... . Omit the second, fourth, ... .
        if round > 0 && round % 4 == 0 && is_local_leader() {
            return Action::Omit;
        }
        if !self.delayed || self.delay_ms == 0 {
            return Action::Propose;
        }
        let mut hasher = Sha512::new();
        hasher.update(b"bullshark-local-proposal-delay-v1");
        hasher.update(self.seed.to_le_bytes());
        hasher.update(self.launch_id.to_le_bytes());
        hasher.update(round.to_le_bytes());
        let sample = u64::from_le_bytes(hasher.finalize()[..8].try_into().unwrap());
        match sample % (self.delay_ms + 1) {
            0 => Action::Propose,
            delay => Action::Wait(delay),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn profile(mode: Mode, delayed: bool) -> ProposalProfile {
        ProposalProfile {
            mode,
            seed: 1,
            launch_id: 3,
            delay_ms: 200,
            delayed,
        }
    }

    #[test]
    fn only_the_regular_leader_omits_every_second_slot_in_both_modes() {
        for profile in &[
            profile(Mode::A3, false),
            profile(Mode::A3, true),
            profile(Mode::A5, true),
        ] {
            for round in 1..=32 {
                for authority in 0..20 {
                    let action = profile.action(round, || authority == round % 20);
                    assert_eq!(
                        action == Action::Omit,
                        round % 4 == 0 && authority == round % 20
                    );
                }
            }
        }
    }

    #[test]
    fn delay_is_repeatable_bounded_and_depends_on_seed_round_and_launch_id() {
        let mut p = profile(Mode::A3, true);
        let schedule =
            |p: &ProposalProfile| (1..=32).map(|r| p.action(r, || false)).collect::<Vec<_>>();
        let first = schedule(&p);
        assert_eq!(first, schedule(&p));
        assert!(first
            .iter()
            .all(|a| matches!(a, Action::Propose | Action::Wait(1..=200))));
        assert!(first.windows(2).any(|v| v[0] != v[1]));
        p.seed += 1;
        assert_ne!(first, schedule(&p));
        p.seed -= 1;
        p.launch_id += 1;
        assert_ne!(first, schedule(&p));
        p.delay_ms = 0;
        assert_eq!(p.action(1, || false), Action::Propose);
    }
}
