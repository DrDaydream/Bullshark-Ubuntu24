//! Replay the offline fixture through four independent in-memory consensus replicas.

#[path = "../tests/fixtures/synthetic_replay.rs"]
mod fixture;

#[tokio::main(flavor = "current_thread")]
async fn main() {
    fixture::replay().await;
}
