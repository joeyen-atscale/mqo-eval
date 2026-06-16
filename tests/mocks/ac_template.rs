//! Hardware mock test template (one file per hardware-deferred AC).
//!
//! Convention (see SKILL.md "Hardware mock convention"): for every AC in
//! the PRD's `deferred_acs:` that is NOT also in `mock_unjustified_for:`,
//! the crate must ship `tests/mocks/ac<N>.rs` (e.g. `tests/mocks/ac3.rs`),
//! copied from this template and filled in. The mock test MUST:
//!
//!   1. exercise the SAME public API surface the real test would — same
//!      call sequence + signatures — so the boundary type-checks exactly
//!      as the real hardware path does;
//!   2. run against a DOCUMENTED in-crate fake (a trait impl, channel
//!      pair, in-memory device, …), never a network or hardware dep;
//!   3. assert the SAME invariant the AC's English text declares.
//!
//! The mock proves the call sequence + signature + invariant *at the type
//! level*. A later `cargo test --features=real-hardware` run proves they
//! hold *in the world*. Both, not either — the mock COMPLEMENTS reality,
//! it does not REPLACE it.
//!
//! Lint parity: this file is held to the same `bad-rust.md` discipline as
//! any other test (`unwrap`/`expect`/`panic` = deny). Surface errors with
//! `assert!`/`assert_eq!` (a failed assert is a test failure, not a
//! production panic) or with `?` on a `-> Result<(), E>` test signature.
//!
//! Mock tests run under plain `cargo test`, so they count toward the
//! Stage 3 `cargo test --workspace` hard gate and toward /build's
//! verified-completed check #5 (OR-clause, path 2).

// --- 1. The boundary trait the real code depends on. -----------------------
// In the real crate this lives in `src/`; the production impl talks to the
// hardware, the mock impl below stands in for it. Replace with the crate's
// actual boundary trait + method signatures.
trait Device {
    /// Mirror the real method signature exactly — same args, same return type.
    fn read_reading(&mut self) -> Result<u32, DeviceError>;
}

#[derive(Debug, PartialEq, Eq)]
enum DeviceError {
    NotReady,
}

// --- 2. The documented in-crate fake. --------------------------------------
// A deterministic stand-in that replays a scripted sequence. Document what
// real behavior each scripted value corresponds to.
struct FakeDevice {
    /// Scripted readings the real PWM/sensor/scheduler would emit, in order.
    script: std::collections::VecDeque<Result<u32, DeviceError>>,
}

impl FakeDevice {
    fn with_script(readings: Vec<Result<u32, DeviceError>>) -> Self {
        Self { script: readings.into_iter().collect() }
    }
}

impl Device for FakeDevice {
    fn read_reading(&mut self) -> Result<u32, DeviceError> {
        // No unwrap: a drained script is itself a meaningful (NotReady) state.
        self.script.pop_front().unwrap_or(Err(DeviceError::NotReady))
    }
}

// --- 3. The mock test: same call sequence + same invariant as the AC. ------
#[test]
fn ac_template_mock() {
    // GIVEN the documented fake scripted with the readings the real device
    // would produce for this AC's scenario.
    let mut dev = FakeDevice::with_script(vec![Ok(42), Ok(43), Err(DeviceError::NotReady)]);

    // WHEN we drive the SAME public call sequence the real test drives.
    let first = dev.read_reading();
    let second = dev.read_reading();
    let exhausted = dev.read_reading();

    // THEN assert the SAME invariant the AC's English text declares.
    // (Example invariant: readings are monotonically non-decreasing while
    //  the device is ready, and exhaustion surfaces as NotReady — replace
    //  with the AC's real invariant.)
    assert_eq!(first, Ok(42), "first reading should match scripted value");
    assert!(
        matches!((first, second), (Ok(a), Ok(b)) if b >= a),
        "readings must be monotonically non-decreasing while ready",
    );
    assert_eq!(exhausted, Err(DeviceError::NotReady), "drained device reports NotReady");
}
