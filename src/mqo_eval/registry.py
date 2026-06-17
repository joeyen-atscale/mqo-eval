"""Agent registry — resolves --agent <name> to a subprocess command."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AgentCapabilities:
    requires_api: bool = False
    returns_handle: bool = False


@dataclass
class AgentEntry:
    name: str
    command: str
    kind: str = "subprocess"
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)


class RegistryError(ValueError):
    pass


def load_registry(path: Path) -> dict[str, AgentEntry]:
    """Load agents.yaml; returns empty dict if file absent."""
    if not path.exists():
        return {}
    raw: Any = yaml.safe_load(path.read_text()) or {}
    agents: dict[str, AgentEntry] = {}
    for name, cfg in raw.get("agents", {}).items():
        caps_raw = cfg.get("capabilities", {})
        agents[name] = AgentEntry(
            name=name,
            command=str(cfg["command"]),
            kind=str(cfg.get("kind", "subprocess")),
            capabilities=AgentCapabilities(
                requires_api=bool(caps_raw.get("requires_api", False)),
                returns_handle=bool(caps_raw.get("returns_handle", False)),
            ),
        )
    return agents


def resolve_agent(name: str, registry: dict[str, AgentEntry]) -> AgentEntry:
    """Resolve --agent <name>; raises RegistryError with known names on miss."""
    if name in registry:
        return registry[name]
    # Fallback: treat as a direct executable path
    p = Path(name)
    if p.exists() and p.is_file():
        return AgentEntry(name=name, command=name)
    known = sorted(registry.keys())
    backends = known if known else "(none registered)"
    raise RegistryError(f"unknown agent {name!r}; known backends: {backends}")
