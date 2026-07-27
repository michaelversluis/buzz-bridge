# buzz-bridge

**Connect any agent or LLM to a [Buzz](https://github.com/block/buzz) channel — in one config file.**

Buzz is a self-hostable workspace where humans and agents share the same rooms.
Its built-in `buzz-acp` harness covers ACP agents (Claude Code, Goose, Codex).
**buzz-bridge** covers everything else: point it at a long-running agent
framework, a plain LLM API, or any HTTP endpoint, and it joins a channel as a
first-class member — reading mentions and posting replies under its own identity.

No protocol code to write, no hardcoded keys or paths. One TOML file describes
the relay, the identity, the channels to watch, how a mention is recognized,
and which agent to invoke.

```
     Buzz channel ──mention──▶ buzz-bridge ──prompt──▶ your agent
          ▲                                                │
          └───────────────── reply ◀───────────────────────┘
```

## Why

Agents in Buzz are members, not bots bolted on the side — own key, own
audit trail. buzz-bridge lets *any* agent take that seat: a self-hosted
a self-hosted host agent, an internal LLM service, or a bare
Claude/OpenAI call. Wire it once; it participates 24/7.

## Install

```bash
git clone https://github.com/<your-org>/buzz-bridge.git && cd buzz-bridge
python3 -m pip install -e .        # Python 3.8+
```

You also need the official [`buzz` CLI](https://github.com/block/buzz) on the
machine (buzz-bridge shells out to it for relay I/O, so it always speaks the
current protocol). The bridge identity must be an admitted relay member.

## Configure

Copy an example from [`examples/`](examples/) and edit it:

```toml
[bridge]
relay_url        = "http://localhost:3000"
private_key      = "env:BUZZ_PRIVATE_KEY"    # or "file:~/.secrets/agent.key", or raw
channels         = ["your-channel-uuid"]
mention_patterns = ["@myagent\\b"]           # regex; omit + respond_to_all=true to answer all
self_pubkey      = "your-agent-pubkey-hex"   # never replies to its own messages
poll_seconds     = 15
agent_pubkeys    = ["other-agent-pubkey"]    # optional: enables the agent-chain cap
max_agent_chain  = 3                          # max consecutive agent messages before silence

[adapter]
kind = "anthropic"                            # cli | http | anthropic
model = "claude-haiku-4-5"
```

Run it:

```bash
python -m buzzbridge run --config myagent.toml     # daemon
python -m buzzbridge once --config myagent.toml    # single pass (cron / testing)
```

## Adapters

| kind | what it does | key options |
|------|--------------|-------------|
| **cli** | Runs a command; `{prompt}` is substituted into argv. Handles TUI agents that print a reply box (`box_agent`) and noisy output (`extract` regex). | `command`, `cwd`, `timeout`, `box_agent`, `extract` |
| **http** | POSTs the prompt to any endpoint; reads the reply from a JSON path. Works with OpenAI-compatible APIs. | `url`, `headers`, `body_template`, `reply_path` |
| **anthropic** | Direct Claude call — an LLM in a channel with zero infra. | `api_key`, `model`, `max_tokens`, `system` |

Adding a harness = one small class in [`buzzbridge/adapters.py`](buzzbridge/adapters.py).
The bridge core never changes.

## Safety by design

- **Mentions only.** Without `respond_to_all`, the bridge answers only messages
  matching your patterns — no accidental chatter, no feedback loops.
- **Never replies to itself** (`self_pubkey`).
- **Agent-to-agent, without runaway loops.** List your other bots in
  `agent_pubkeys` and agents may mention each other — useful for relaying
  tasks — but once `max_agent_chain` consecutive messages are agent-authored
  the bridge goes silent until a human speaks again. Two bridges can never
  ping-pong forever.
- **Idempotent.** A per-channel state file tracks the last message seen, so a
  restart never re-answers old messages.
- **Secrets stay out of the repo.** Keys are referenced via `env:` / `file:`.

## Status

Early but working: cli/http/anthropic adapters, mention handling, multi-channel,
idempotent state, tests. Built and run in production against a self-hosted Buzz
relay. Issues and PRs welcome.

## License

Apache-2.0 — matching Buzz.
