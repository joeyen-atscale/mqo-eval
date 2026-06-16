//! Mock tests entry point — wires up hardware-deferred AC mocks.
//!
//! Each `mod` here pulls in `tests/mocks/<name>.rs`.
//! All mock tests run under plain `cargo test`.

#[path = "mocks/ac_template.rs"]
mod ac_template;
