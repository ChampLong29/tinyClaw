"""5-tier routing, binding table, and agent management."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


VALID_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
INVALID_CHARS_RE = re.compile(r"[^a-z0-9_-]+")
DEFAULT_AGENT_ID = "main"
DEFAULT_AGENTS_DIR = Path("workspace") / ".agents"


def normalize_agent_id(value: str) -> str:
    """Normalize and validate an agent ID."""
    trimmed = value.strip()
    if not trimmed:
        return DEFAULT_AGENT_ID
    if VALID_ID_RE.match(trimmed):
        return trimmed.lower()
    cleaned = INVALID_CHARS_RE.sub("-", trimmed.lower()).strip("-")[:64]
    return cleaned or DEFAULT_AGENT_ID


@dataclass
class Binding:
    """A routing binding: match_key=match_value -> agent_id."""
    agent_id: str
    tier: int          # 1=peer, 2=guild, 3=account, 4=channel, 5=default
    match_key: str     # "peer_id" | "guild_id" | "account_id" | "channel" | "default"
    match_value: str    # e.g. "telegram:12345", "telegram", "*"
    priority: int = 0

    def display(self) -> str:
        names = {1: "peer", 2: "guild", 3: "account", 4: "channel", 5: "default"}
        label = names.get(self.tier, f"tier-{self.tier}")
        return f"[{label}] {self.match_key}={self.match_value} -> agent:{self.agent_id} (pri={self.priority})"


class BindingTable:
    """5-tier routing binding table."""

    def __init__(self) -> None:
        self._bindings: list[Binding] = []

    def add(self, binding: Binding) -> None:
        self._bindings.append(binding)
        self._bindings.sort(key=lambda b: (b.tier, -b.priority))

    def remove(self, agent_id: str, match_key: str, match_value: str) -> bool:
        before = len(self._bindings)
        self._bindings = [
            b for b in self._bindings
            if not (b.agent_id == agent_id and b.match_key == match_key
                    and b.match_value == match_value)
        ]
        return len(self._bindings) < before

    def list_all(self) -> list[Binding]:
        return list(self._bindings)

    def resolve(
        self,
        channel: str = "",
        account_id: str = "",
        guild_id: str = "",
        peer_id: str = "",
    ) -> tuple[str | None, Binding | None]:
        """Resolve (agent_id, matched_binding) from input attributes."""
        for b in self._bindings:
            if b.tier == 1 and b.match_key == "peer_id":
                if ":" in b.match_value:
                    if b.match_value == f"{channel}:{peer_id}":
                        return b.agent_id, b
                elif b.match_value == peer_id:
                    return b.agent_id, b
            elif b.tier == 2 and b.match_key == "guild_id" and b.match_value == guild_id:
                return b.agent_id, b
            elif b.tier == 3 and b.match_key == "account_id" and b.match_value == account_id:
                return b.agent_id, b
            elif b.tier == 4 and b.match_key == "channel" and b.match_value == channel:
                return b.agent_id, b
            elif b.tier == 5 and b.match_key == "default":
                return b.agent_id, b
        return None, None


@dataclass
class AgentConfig:
    """Configuration for an agent."""
    id: str
    name: str
    personality: str = ""
    model: str = ""
    dm_scope: str = "per-peer"  # "main" | "per-peer" | "per-channel-peer" | "per-account-channel-peer"

    @property
    def effective_model(self) -> str:
        return self.model or "claude-sonnet-4-20250514"

    def system_prompt(self) -> str:
        parts = [f"You are {self.name}."]
        if self.personality:
            parts.append(f"Your personality: {self.personality}")
        parts.append("Answer questions helpfully and stay in character.")
        return " ".join(parts)


class AgentManager:
    """Manages agent configs and session state."""

    def __init__(self, agents_base: Path | None = None) -> None:
        self._agents: dict[str, AgentConfig] = {}
        self._agents_base = agents_base or DEFAULT_AGENTS_DIR
        self._sessions: dict[str, list[dict]] = {}

    def register(self, config: AgentConfig) -> None:
        aid = normalize_agent_id(config.id)
        config.id = aid
        self._agents[aid] = config
        agent_dir = self._agents_base / aid
        (agent_dir / "sessions").mkdir(parents=True, exist_ok=True)

    def get_agent(self, agent_id: str) -> AgentConfig | None:
        return self._agents.get(normalize_agent_id(agent_id))

    def list_agents(self) -> list[AgentConfig]:
        return list(self._agents.values())

    def get_session(self, session_key: str) -> list[dict]:
        if session_key not in self._sessions:
            self._sessions[session_key] = []
        return self._sessions[session_key]

    def list_sessions(self, agent_id: str = "") -> dict[str, int]:
        aid = normalize_agent_id(agent_id) if agent_id else ""
        return {k: len(v) for k, v in self._sessions.items()
                if not aid or k.startswith(f"agent:{aid}:")}


def build_session_key(
    agent_id: str,
    channel: str = "",
    account_id: str = "",
    peer_id: str = "",
    dm_scope: str = "per-peer",
) -> str:
    """Build a session isolation key based on dm_scope."""
    aid = normalize_agent_id(agent_id)
    ch = (channel or "unknown").strip().lower()
    acc = (account_id or "default").strip().lower()
    pid = (peer_id or "").strip().lower()
    if dm_scope == "per-account-channel-peer" and pid:
        return f"agent:{aid}:{ch}:{acc}:direct:{pid}"
    if dm_scope == "per-channel-peer" and pid:
        return f"agent:{aid}:{ch}:direct:{pid}"
    if dm_scope == "per-peer" and pid:
        return f"agent:{aid}:direct:{pid}"
    return f"agent:{aid}:main"


def resolve_route(
    bindings: BindingTable,
    mgr: AgentManager,
    channel: str,
    peer_id: str,
    account_id: str = "",
    guild_id: str = "",
) -> tuple[str, str]:
    """Resolve route: (agent_id, session_key)."""
    agent_id, matched = bindings.resolve(
        channel=channel, account_id=account_id,
        guild_id=guild_id, peer_id=peer_id,
    )
    if not agent_id:
        agent_id = DEFAULT_AGENT_ID
    agent = mgr.get_agent(agent_id)
    dm_scope = agent.dm_scope if agent else "per-peer"
    sk = build_session_key(
        agent_id, channel=channel, account_id=account_id,
        peer_id=peer_id, dm_scope=dm_scope,
    )
    return agent_id, sk
