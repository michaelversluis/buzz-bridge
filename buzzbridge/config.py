"""Configuration loading for buzz-bridge.

A bridge instance is described by a single TOML file. Everything that is
site-specific — relay URL, identity key, which channels to watch, how a
mention is recognized, and which agent to invoke — lives there, so the code
carries no hardcoded paths or keys.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:                              # Python 3.11+
    import tomllib
except ModuleNotFoundError:       # 3.8–3.10: pip install tomli
    import tomli as tomllib


@dataclass
class AdapterConfig:
    """How to reach the agent. `kind` selects the adapter implementation."""
    kind: str                                  # "cli" | "http" | "anthropic"
    options: dict = field(default_factory=dict)


@dataclass
class Config:
    relay_url: str
    private_key: str                           # hex or nsec (resolved, see load)
    channels: list[str]
    adapter: AdapterConfig
    buzz_cli: str = "buzz"                     # path to the `buzz` binary
    mention_patterns: list[str] = field(default_factory=list)
    respond_to_all: bool = False               # if true, ignore mention_patterns
    self_pubkey: str | None = None             # never reply to own messages
    poll_seconds: int = 15
    state_file: str = "buzzbridge-state.json"
    max_prompt_chars: int = 8000
    agent_pubkeys: list[str] = field(default_factory=list)  # other agents in the channel
    max_agent_chain: int = 3                   # cap on consecutive agent-authored messages
    silence_token: str | None = None           # agent reply that means "post nothing"
    context_messages: int = 0                  # recent channel messages included in the prompt
    thread_replies: bool = False               # reply in a thread under the mention
    display_names: dict = field(default_factory=dict)  # pubkey -> label for context rendering

    @staticmethod
    def _resolve_secret(value: str) -> str:
        """Allow `env:VAR` or `file:/path` indirection so keys stay out of the TOML."""
        if value.startswith("env:"):
            v = os.environ.get(value[4:], "")
            if not v:
                raise ValueError("env var %s is empty" % value[4:])
            return v.strip()
        if value.startswith("file:"):
            return open(os.path.expanduser(value[5:])).read().strip()
        return value

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        b = raw.get("bridge", {})
        a = raw.get("adapter", {})
        if not a.get("kind"):
            raise ValueError("config: [adapter] kind is required")
        channels = b.get("channels") or []
        if isinstance(channels, str):
            channels = [channels]
        if not channels:
            raise ValueError("config: [bridge] channels is required")
        pk = cls._resolve_secret(b["private_key"]) if b.get("private_key") else None
        if not pk:
            raise ValueError("config: [bridge] private_key is required")
        return cls(
            relay_url=b.get("relay_url") or os.environ.get("BUZZ_RELAY_URL", ""),
            private_key=pk,
            channels=channels,
            adapter=AdapterConfig(kind=a["kind"],
                                  options={k: v for k, v in a.items() if k != "kind"}),
            buzz_cli=os.path.expanduser(b.get("buzz_cli", "buzz")),
            mention_patterns=b.get("mention_patterns", []),
            respond_to_all=bool(b.get("respond_to_all", False)),
            self_pubkey=b.get("self_pubkey"),
            poll_seconds=int(b.get("poll_seconds", 15)),
            state_file=os.path.expanduser(b.get("state_file", "buzzbridge-state.json")),
            max_prompt_chars=int(b.get("max_prompt_chars", 8000)),
            agent_pubkeys=b.get("agent_pubkeys", []),
            max_agent_chain=int(b.get("max_agent_chain", 3)),
            silence_token=b.get("silence_token"),
            context_messages=int(b.get("context_messages", 0)),
            thread_replies=bool(b.get("thread_replies", False)),
            display_names=b.get("display_names", {}),
        )
