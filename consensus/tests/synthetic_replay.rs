#[path = "fixtures/synthetic_replay.rs"]
mod fixture;

#[tokio::test]
#[ignore = "opt-in replay uses the Python benchmark model"]
async fn replay_synthetic_schedules() {
    fixture::replay().await;
}
