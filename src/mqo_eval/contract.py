"""AgentAnswer contract — typed union with JSON Schema generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError


class TabularAnswer(BaseModel):
    answer_type: Literal["tabular"] = "tabular"
    columns: list[str]
    rows: list[list[Any]]


class HandleAnswer(BaseModel):
    answer_type: Literal["handle"] = "handle"
    handle_id: str
    resolve: dict[str, Any] = {}


class ScalarAnswer(BaseModel):
    answer_type: Literal["scalar"] = "scalar"
    value: Any


class CannotAnswer(BaseModel):
    answer_type: Literal["cannot_answer"] = "cannot_answer"
    reason: str = ""


AgentAnswer = TabularAnswer | HandleAnswer | ScalarAnswer | CannotAnswer

_DISCRIMINATOR = "answer_type"
_VARIANTS: dict[str, type[BaseModel]] = {
    "tabular": TabularAnswer,
    "handle": HandleAnswer,
    "scalar": ScalarAnswer,
    "cannot_answer": CannotAnswer,
}


class ParseError(ValueError):
    """Raised when agent output cannot be parsed into a valid AgentAnswer."""


def parse_answer(raw: str) -> AgentAnswer:
    """Parse agent stdout into an AgentAnswer; raises ParseError on invalid input."""
    raw = raw.strip()
    if not raw:
        raise ParseError("agent produced no output")
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(f"agent output is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        t = type(data).__name__
        raise ParseError(f"agent output must be a JSON object, got {t}")

    answer_type = data.get(_DISCRIMINATOR)
    if answer_type not in _VARIANTS:
        # Legacy compat: bare scalar / bound_mqo
        if "bound_mqo" in data or "result_rows" in data:
            rows = data.get("result_rows") or []
            if rows and isinstance(rows[0], list):
                return TabularAnswer(columns=[], rows=rows)
            val = data.get("bound_mqo") or (rows[0][0] if rows else None)
            return ScalarAnswer(value=val)
        raise ParseError(
            f"unknown answer_type {answer_type!r}; expected one of {list(_VARIANTS)}"
        )

    cls = _VARIANTS[answer_type]
    try:
        return cls.model_validate(data)  # type: ignore[return-value]
    except ValidationError as exc:
        raise ParseError(f"invalid {answer_type} envelope: {exc}") from exc


def emit_schema(dest: Path) -> None:
    """Write the AgentAnswer JSON Schema to *dest*."""
    import pydantic.json_schema as pjs

    combined = pjs.models_json_schema(
        [
            (TabularAnswer, "validation"),
            (HandleAnswer, "validation"),
            (ScalarAnswer, "validation"),
            (CannotAnswer, "validation"),
        ],
        title="AgentAnswer",
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(combined[1], indent=2))
