//! mqo-eval — LLM-free eval harness driven by mqo-agent.
//!
//! Drives question sets through `mqo-agent` (or a stub binder in CI) and
//! grades results without requiring any LLM API key.

#![cfg_attr(not(test), forbid(unsafe_code))]
#![warn(missing_docs)]

pub mod compare;
pub mod oracle;
pub mod run;
pub mod summary;
pub mod types;
