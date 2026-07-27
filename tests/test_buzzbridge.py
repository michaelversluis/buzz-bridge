import json

from buzzbridge.adapters import CliAdapter, _extract_box, _dig, build_adapter
from buzzbridge.bridge import Bridge
from buzzbridge.config import Config, AdapterConfig


def _cfg(**kw):
    base = dict(relay_url="http://x:3100", private_key="nsecdummy",
                channels=["chan-1"], adapter=AdapterConfig("cli", {"command": ["true"]}),
                mention_patterns=[r"@bot\b", r"\bbot[,:]"], self_pubkey="SELF")
    base.update(kw)
    return Config(**base)


class FakeAdapter:
    def __init__(self, reply="pong"):
        self.reply = reply
        self.seen = []

    def ask(self, prompt):
        self.seen.append(prompt)
        return self.reply


def test_config_load_and_secret_indirection(tmp_path, monkeypatch):
    monkeypatch.setenv("MYKEY", "nsec-from-env")
    cfg_file = tmp_path / "bridge.toml"
    cfg_file.write_text(
        '[bridge]\n'
        'relay_url = "http://relay:3100"\n'
        'private_key = "env:MYKEY"\n'
        'channels = "abc"\n'
        'mention_patterns = ["@bot\\\\b"]\n'
        'self_pubkey = "SELF"\n'
        '[adapter]\n'
        'kind = "cli"\n'
        'command = ["echo", "{prompt}"]\n')
    cfg = Config.load(str(cfg_file))
    assert cfg.private_key == "nsec-from-env"
    assert cfg.channels == ["abc"]
    assert cfg.adapter.kind == "cli"
    assert cfg.adapter.options["command"] == ["echo", "{prompt}"]


def test_trigger_and_prompt_cleaning():
    b = Bridge(_cfg(), adapter=FakeAdapter())
    assert b.is_trigger("hey @bot what's up")
    assert b.is_trigger("bot: status?")
    assert not b.is_trigger("just chatting")
    assert b.clean_prompt("@bot what is 2+2") == "what is 2+2"


def test_respond_to_all_overrides_patterns():
    b = Bridge(_cfg(respond_to_all=True), adapter=FakeAdapter())
    assert b.is_trigger("anything at all")


def test_process_once_only_answers_new_mentions(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state)), adapter=FakeAdapter("42"))
    msgs = [
        {"created_at": 100, "pubkey": "someone", "content": "@bot question one"},
        {"created_at": 101, "pubkey": "SELF", "content": "@bot my own message"},  # skip: self
        {"created_at": 102, "pubkey": "someone", "content": "no mention here"},   # skip: no trigger
    ]
    sent = []
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in msgs if m["created_at"] > since])
    monkeypatch.setattr(b, "_send", lambda ch, text: sent.append((ch, text)))
    # first pass: since defaults to ~now, so nothing is newer -> seed state back
    b._save_state({"chan-1": 99})
    b.process_once()
    assert sent == [("chan-1", "42")]
    assert b.adapter.seen == ["question one"]
    # second pass: nothing new -> no duplicate answer
    b.process_once()
    assert len(sent) == 1


def test_agent_chain_allows_short_relay_blocks_long(tmp_path, monkeypatch):
    """Agent A may relay to agent B, but a runaway A↔B ping-pong is cut off."""
    state = tmp_path / "state.json"
    cfg = _cfg(state_file=str(state), agent_pubkeys=["AGENT-A", "AGENT-B"],
               max_agent_chain=3)
    b = Bridge(cfg, adapter=FakeAdapter("ack"))
    history = [
        {"created_at": 100, "pubkey": "human", "content": "please ask the other bot"},
        {"created_at": 101, "pubkey": "AGENT-A", "content": "@bot can you check X"},
    ]
    sent = []
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in history if m["created_at"] > since])
    monkeypatch.setattr(b, "_recent", lambda ch, limit=10: sorted(
        history, key=lambda m: m["created_at"], reverse=True))
    monkeypatch.setattr(b, "_send", lambda ch, text: sent.append(text))
    b._save_state({"chan-1": 100})
    b.process_once()          # chain is 1 (< 3) -> reply
    assert sent == ["ack"]
    # now the tail of the channel is agent-only and long enough to trip the cap
    history += [
        {"created_at": 102, "pubkey": "AGENT-B", "content": "@bot and back to you"},
        {"created_at": 103, "pubkey": "AGENT-A", "content": "@bot once more"},
    ]
    b._save_state({"chan-1": 102})
    b.process_once()          # chain is 3 (>= 3) -> silence
    assert sent == ["ack"]


def test_agent_chain_resets_on_human_message(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    cfg = _cfg(state_file=str(state), agent_pubkeys=["AGENT-A", "AGENT-B"],
               max_agent_chain=3)
    b = Bridge(cfg, adapter=FakeAdapter())
    history = [
        {"created_at": 100, "pubkey": "AGENT-A", "content": "earlier bot talk"},
        {"created_at": 101, "pubkey": "AGENT-B", "content": "more bot talk"},
        {"created_at": 102, "pubkey": "human", "content": "thanks!"},
        {"created_at": 103, "pubkey": "AGENT-A", "content": "@bot new request"},
    ]
    monkeypatch.setattr(b, "_recent", lambda ch, limit=10: sorted(
        history, key=lambda m: m["created_at"], reverse=True))
    assert b.agent_chain_len("chan-1", history[-1]) == 1  # human at 102 resets


def test_empty_agent_pubkeys_keeps_old_behavior(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state)), adapter=FakeAdapter("ok"))
    sent = []
    msgs = [{"created_at": 101, "pubkey": "AGENT-A", "content": "@bot hi"}]
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in msgs if m["created_at"] > since])
    monkeypatch.setattr(b, "_send", lambda ch, text: sent.append(text))
    b._save_state({"chan-1": 100})
    b.process_once()
    assert sent == ["ok"]     # no agent list configured -> everyone is answered


def test_cli_adapter_box_extraction():
    out = ("Initializing agent...\n"
           "╭─ ⚕ Hermes ───────────────╮\n"
           "  Ja, ik ben bereikbaar.\n"
           "╰──────────────────────────╯\n"
           "Resume this session with: ...\n")
    assert _extract_box(out, "Hermes") == "Ja, ik ben bereikbaar."


def test_cli_adapter_runs_command():
    a = CliAdapter({"command": ["printf", "%s", "hello {prompt}"]})
    assert a.ask("world") == "hello world"


def test_http_dig_path():
    payload = {"choices": [{"message": {"content": "hi there"}}]}
    assert _dig(payload, "choices.0.message.content") == "hi there"


def test_unknown_adapter_raises():
    try:
        build_adapter("nope", {})
        assert False
    except ValueError as e:
        assert "unknown adapter" in str(e)
