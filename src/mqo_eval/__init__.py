"""mqo-eval: API-free, handle-scoring MQO eval harness."""

__version__ = "0.1.0"

# Rule-violation harness (iter-1: deterministic sql-only checks)
from .rule_harness import RuleViolationHarness, RuleVerdict, is_live_eval_running  # noqa: F401
