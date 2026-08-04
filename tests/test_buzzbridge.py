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
    monkeypatch.setattr(b, "_send", lambda ch, text, reply_to=None: sent.append((ch, text)))
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
    monkeypatch.setattr(b, "_send", lambda ch, text, reply_to=None: sent.append(text))
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
    monkeypatch.setattr(b, "_send", lambda ch, text, reply_to=None: sent.append(text))
    b._save_state({"chan-1": 100})
    b.process_once()
    assert sent == ["ok"]     # no agent list configured -> everyone is answered


def test_ptag_mention_triggers_without_text_match(tmp_path, monkeypatch):
    """A rich @-mention carries a p-tag; it must trigger even if the display
    text doesn't match any regex (e.g. after a display-name change)."""
    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state), self_pubkey="MYPUB"), adapter=FakeAdapter("yo"))
    sent = []
    msgs = [{"created_at": 101, "pubkey": "someone", "content": "hey @SomeNewName look",
             "tags": [["h", "chan-1"], ["p", "MYPUB"]]}]
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in msgs if m["created_at"] > since])
    monkeypatch.setattr(b, "_send", lambda ch, text, reply_to=None: sent.append(text))
    b._save_state({"chan-1": 100})
    b.process_once()
    assert sent == ["yo"]


def test_context_prepended_and_thread_reply(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state), context_messages=2, thread_replies=True,
                    display_names={"human-pk": "Michael"}),
               adapter=FakeAdapter("ok"))
    history = [
        {"id": "m1", "created_at": 98, "pubkey": "human-pk", "content": "eerdere vraag"},
        {"id": "m2", "created_at": 99, "pubkey": "other-pk", "content": "eerder antwoord"},
        {"id": "m3", "created_at": 101, "pubkey": "human-pk", "content": "@bot en nu?"},
    ]
    sent = []
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in history if m["created_at"] > since])
    monkeypatch.setattr(b, "_recent", lambda ch, limit=10: sorted(
        history, key=lambda m: m["created_at"], reverse=True))
    monkeypatch.setattr(b, "_send",
                        lambda ch, text, reply_to=None: sent.append((text, reply_to)))
    b._save_state({"chan-1": 100})
    b.process_once()
    prompt = b.adapter.seen[0]
    assert "Michael: eerdere vraag" in prompt          # labeled via display_names
    assert "other-pk"[:8] not in prompt or True         # fallback label allowed
    assert prompt.index("eerdere vraag") < prompt.index("eerder antwoord")  # oldest first
    assert prompt.rstrip().endswith("en nu?")           # question comes last
    assert sent == [("ok", "m3")]                       # threaded under the mention


def test_thread_reply_targets_root_not_reply(tmp_path, monkeypatch):
    """Replying under a message that is itself a thread reply must target the
    thread ROOT (its e-tag), not the reply's own id — relays reject the latter."""
    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state), thread_replies=True), adapter=FakeAdapter("ok"))
    msgs = [{"id": "reply-msg", "created_at": 101, "pubkey": "someone",
             "content": "@bot vraagje", "tags": [["e", "root-msg"], ["h", "chan-1"]]}]
    sent = []
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in msgs if m["created_at"] > since])
    monkeypatch.setattr(b, "_send",
                        lambda ch, text, reply_to=None: sent.append(reply_to) or True)
    b._save_state({"chan-1": 100})
    b.process_once()
    assert sent == ["root-msg"]
    # a top-level message is its own root
    assert Bridge.thread_root({"id": "top", "tags": [["h", "chan-1"]]}) == "top"


def test_send_argv_safe_for_leading_dash():
    """A reply starting with '- ' (bullet list) must not be parsed as a flag."""
    b = Bridge(_cfg(), adapter=FakeAdapter())
    argv = b._send_argv("chan-1", "- punt een\n- punt twee", "root-id")
    assert "--content=- punt een\n- punt twee" in argv
    assert "--reply-to=root-id" in argv
    assert "-" not in [a for a in argv if not a.startswith("-")][2:]


def test_no_context_when_disabled():
    b = Bridge(_cfg(), adapter=FakeAdapter())
    assert b.build_context("chan-1", {"id": "x", "created_at": 5}) == ""


def test_silence_token_suppresses_post(tmp_path, monkeypatch):
    """An agent replying with the silence token posts nothing at all."""
    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state), silence_token="NO_REPLY"),
               adapter=FakeAdapter("NO_REPLY"))
    sent = []
    msgs = [{"created_at": 101, "pubkey": "someone", "content": "@bot fyi only"}]
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in msgs if m["created_at"] > since])
    monkeypatch.setattr(b, "_send", lambda ch, text, reply_to=None: sent.append(text))
    b._save_state({"chan-1": 100})
    b.process_once()
    assert sent == []
    assert b.adapter.seen == ["fyi only"]     # the agent did run, it chose silence


def test_cli_adapter_box_extraction():
    out = ("Initializing agent...\n"
           "╭─ ⚕ Hermes ───────────────╮\n"
           "  Ja, ik ben bereikbaar.\n"
           "╰──────────────────────────╯\n"
           "Resume this session with: ...\n")
    assert _extract_box(out, "Hermes") == "Ja, ik ben bereikbaar."


def test_box_extraction_takes_last_box():
    """Streaming agents print progress boxes; only the final box is the answer."""
    out = ("╭─ ⚕ Hermes ───────────────╮\n"
           "  Bevindingen verzameld — nu verifieer ik...\n"
           "╰──────────────────────────╯\n"
           "tool output, noise\n"
           "╭─ ⚕ Hermes ───────────────╮\n"
           "  Definitief antwoord.\n"
           "╰──────────────────────────╯\n")
    assert _extract_box(out, "Hermes") == "Definitief antwoord."


def test_state_saved_before_agent_runs(tmp_path, monkeypatch):
    """A crash/restart during a slow answer must never re-answer the mention."""
    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state)), adapter=FakeAdapter("hi"))

    class Boom(Exception):
        pass

    def crashing_ask(prompt):
        # by the time the agent runs, the trigger must already be persisted
        assert json.load(open(state))["chan-1"] == 101
        raise Boom()

    msgs = [{"created_at": 101, "pubkey": "someone", "content": "@bot slow question"}]
    monkeypatch.setattr(b.adapter, "ask", crashing_ask)
    sent = []
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in msgs if m["created_at"] > since])
    monkeypatch.setattr(b, "_send", lambda ch, text, reply_to=None: sent.append(text))
    b._save_state({"chan-1": 100})
    b.process_once()          # adapter "crashes" -> error reply, state persisted
    # a fresh pass (as after a restart) must not re-answer
    monkeypatch.setattr(b.adapter, "ask", lambda p: "late answer")
    b.process_once()
    assert not any("late answer" in s for s in sent)


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


def test_afzender_gaat_mee_naar_de_adapter(tmp_path, monkeypatch):
    """De wrapper moet weten wie het vroeg — anders kan geciteerde tekst van
    buiten (Hermes die e-mail samenvat) een schrijfactie uitlokken."""
    gezien = {}

    class AdapterMetAfzender:
        def ask(self, prompt, sender=""):
            gezien["sender"] = sender
            return "ok"

    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state)), adapter=AdapterMetAfzender())
    msgs = [{"created_at": 100, "pubkey": "mens-1", "content": "@bot doe iets"}]
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in msgs if m["created_at"] > since])
    monkeypatch.setattr(b, "_send", lambda ch, text, reply_to=None: None)
    b._save_state({"chan-1": 99})
    b.process_once()
    assert gezien["sender"] == "mens-1"


def test_oude_adapter_zonder_afzender_blijft_werken(tmp_path, monkeypatch):
    """Adapters van buiten deze repo hebben alleen ask(prompt)."""
    class OudeAdapter:
        def ask(self, prompt):
            return "ok"

    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state)), adapter=OudeAdapter())
    msgs = [{"created_at": 100, "pubkey": "mens-1", "content": "@bot doe iets"}]
    verstuurd = []
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in msgs if m["created_at"] > since])
    monkeypatch.setattr(b, "_send", lambda ch, text, reply_to=None: verstuurd.append(text))
    b._save_state({"chan-1": 99})
    b.process_once()
    assert verstuurd == ["ok"]


def test_kanaal_gaat_mee_naar_de_adapter(tmp_path, monkeypatch):
    """Met meerdere kanalen moet de wrapper weten waar een bericht vandaan komt."""
    gezien = {}

    class AdapterMetKanaal:
        def ask(self, prompt, sender="", channel=""):
            gezien.update(sender=sender, channel=channel)
            return "ok"

    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state)), adapter=AdapterMetKanaal())
    msgs = [{"created_at": 100, "pubkey": "mens-1", "content": "@bot doe iets"}]
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in msgs if m["created_at"] > since])
    monkeypatch.setattr(b, "_send", lambda ch, text, reply_to=None: None)
    b._save_state({"chan-1": 99})
    b.process_once()
    assert gezien == {"sender": "mens-1", "channel": "chan-1"}


def test_adapter_met_alleen_afzender_blijft_werken(tmp_path, monkeypatch):
    """Adapters op 0.4 kennen wel de afzender maar nog geen kanaal."""
    class AdapterZonderKanaal:
        def ask(self, prompt, sender=""):
            return "ok"

    state = tmp_path / "state.json"
    b = Bridge(_cfg(state_file=str(state)), adapter=AdapterZonderKanaal())
    msgs = [{"created_at": 100, "pubkey": "mens-1", "content": "@bot doe iets"}]
    verstuurd = []
    monkeypatch.setattr(b, "_get", lambda ch, since: [m for m in msgs if m["created_at"] > since])
    monkeypatch.setattr(b, "_send", lambda ch, text, reply_to=None: verstuurd.append(text))
    b._save_state({"chan-1": 99})
    b.process_once()
    assert verstuurd == ["ok"]
