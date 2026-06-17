"""Stub agent — returns a deterministic cannot_answer for every question.

Used as the built-in self-test backend; requires no API, no network.
Invoke directly: python -m mqo_eval.stub_agent
"""

from __future__ import annotations

import json


def main() -> None:
    print(json.dumps({"answer_type": "cannot_answer", "reason": "stub agent"}))


if __name__ == "__main__":
    main()
