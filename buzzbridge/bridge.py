"""The bridge core: poll channels, detect mentions, ask the agent, reply.

Relay I/O goes through the official `buzz` CLI (JSON in/out), so buzz-bridge
never reimplements the Nostr/Buzz protocol and stays correct as Buzz evolves.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

from .adapters import Adapter, build_adapter
from .config import Config


class Bridge:
    def __init__(self, cfg: Config, adapter: Adapter | None = None):
        self.cfg = cfg
        self.adapter = adapter or build_adapter(cfg.adapter.kind, cfg.adapter.options)
        self._patterns = [re.compile(p, re.I) for p in cfg.mention_patterns]

    # ── relay wrappers ─────────────────────────────────────────────────
    def _env(self):
        e = dict(os.environ)
        e["BUZZ_RELAY_URL"] = self.cfg.relay_url
        e["BUZZ_PRIVATE_KEY"] = self.cfg.private_key
        return e

    def _log(self, msg: str):
        print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg), flush=True)

    def _get(self, channel: str, since: int):
        r = subprocess.run(
            [self.cfg.buzz_cli, "messages", "get", "--channel", channel,
             "--since", str(since), "--limit", "50", "--kinds", "9"],
            capture_output=True, text=True, env=self._env(), timeout=60)
        if r.returncode != 0:
            print("%s get failed: %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                         r.stderr.strip()[:200]),
                  file=sys.stderr, flush=True)
            return []
        try:
            return sorted(json.loads(r.stdout or "[]"),
                          key=lambda m: m.get("created_at", 0))
        except ValueError:
            return []

    def _recent(self, channel: str, limit: int = 10):
        """Latest messages in a channel, newest first (no since-filter)."""
        r = subprocess.run(
            [self.cfg.buzz_cli, "messages", "get", "--channel", channel,
             "--limit", str(limit), "--kinds", "9"],
            capture_output=True, text=True, env=self._env(), timeout=60)
        if r.returncode != 0:
            return []
        try:
            return sorted(json.loads(r.stdout or "[]"),
                          key=lambda m: m.get("created_at", 0), reverse=True)
        except ValueError:
            return []

    def _send(self, channel: str, text: str, reply_to: str | None = None):
        argv = [self.cfg.buzz_cli, "messages", "send", "--channel", channel,
                "--content", text]
        if reply_to:
            argv += ["--reply-to", reply_to]
        subprocess.run(argv, capture_output=True, text=True,
                       env=self._env(), timeout=60)

    # ── state ──────────────────────────────────────────────────────────
    def _load_state(self):
        try:
            return json.load(open(self.cfg.state_file))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state):
        os.makedirs(os.path.dirname(self.cfg.state_file) or ".", exist_ok=True)
        tmp = self.cfg.state_file + ".tmp"
        json.dump(state, open(tmp, "w"))
        os.replace(tmp, self.cfg.state_file)

    # ── logic ──────────────────────────────────────────────────────────
    def is_trigger(self, text: str) -> bool:
        if self.cfg.respond_to_all:
            return True
        return any(p.search(text or "") for p in self._patterns)

    def is_ptag_mention(self, msg: dict) -> bool:
        """Rich @-mentions carry a p-tag with the mentioned pubkey — more
        precise than text matching, and they survive display-name changes."""
        if not self.cfg.self_pubkey:
            return False
        return any(t[0] == "p" and len(t) > 1 and t[1] == self.cfg.self_pubkey
                   for t in msg.get("tags", []) if isinstance(t, list) and t)

    def _label(self, pubkey: str) -> str:
        return self.cfg.display_names.get(pubkey, pubkey[:8])

    def build_context(self, channel: str, trigger: dict) -> str:
        """Last N channel messages before the trigger, oldest first, labeled."""
        n = self.cfg.context_messages
        if n <= 0:
            return ""
        ts = trigger.get("created_at", 0)
        tid = trigger.get("id")
        recent = [m for m in self._recent(channel, limit=n + 5)
                  if m.get("created_at", 0) <= ts and m.get("id") != tid][:n]
        if not recent:
            return ""
        lines = ["%s: %s" % (self._label(m.get("pubkey", "")),
                             (m.get("content") or "").strip())
                 for m in reversed(recent)]
        return ("Recent channel history (oldest first):\n%s\n\n"
                "Message addressed to you:\n" % "\n".join(lines))

    def agent_chain_len(self, channel: str, trigger: dict) -> int:
        """Consecutive agent-authored messages ending at `trigger`.

        Agent-to-agent relays are allowed, but every hop grows the chain; a
        human message resets it. Once the chain reaches max_agent_chain the
        bridge stays silent, so two bridges can never ping-pong forever.
        """
        agents = set(self.cfg.agent_pubkeys)
        ts = trigger.get("created_at", 0)
        chain = 0
        for m in self._recent(channel):
            if m.get("created_at", 0) > ts:
                continue
            if m.get("pubkey") in agents:
                chain += 1
            else:
                break
        return chain

    def clean_prompt(self, text: str) -> str:
        p = text
        for pat in self._patterns:
            p = pat.sub("", p)
        return (p.strip() or text)[: self.cfg.max_prompt_chars]

    def process_once(self):
        state = self._load_state()
        for channel in self.cfg.channels:
            since = int(state.get(channel, int(time.time())))
            newest = since
            for m in self._get(channel, since):
                ts = m.get("created_at", 0)
                newest = max(newest, ts)
                if ts <= since:
                    continue
                if self.cfg.self_pubkey and m.get("pubkey") == self.cfg.self_pubkey:
                    continue
                text = m.get("content", "")
                if not (self.is_trigger(text) or self.is_ptag_mention(m)):
                    continue
                if (self.cfg.agent_pubkeys
                        and m.get("pubkey") in self.cfg.agent_pubkeys
                        and self.agent_chain_len(channel, m) >= self.cfg.max_agent_chain):
                    continue
                # Mark this message processed *before* the agent runs: answers
                # can take minutes, and a crash/restart mid-answer must never
                # cause the same mention to be answered twice.
                state[channel] = max(int(state.get(channel, 0)), ts)
                self._save_state(state)
                prompt = self.build_context(channel, m) + self.clean_prompt(text)
                reply = self._answer(prompt)
                if (reply and self.cfg.silence_token
                        and reply.strip() == self.cfg.silence_token):
                    self._log("declined %s" % (m.get("id") or "")[:8])
                    continue          # the agent decided this wasn't for it
                if reply:
                    self._send(channel, reply,
                               reply_to=m.get("id") if self.cfg.thread_replies else None)
                    self._log("answered %s" % (m.get("id") or "")[:8])
            state[channel] = max(int(state.get(channel, 0)), newest)
        self._save_state(state)

    def _answer(self, prompt: str) -> str:
        try:
            return self.adapter.ask(prompt) or "(no answer)"
        except subprocess.TimeoutExpired:
            return "⏳ That took too long — try again or split the question."
        except Exception as e:  # noqa: BLE001
            return "⚠️ Agent error: %s" % str(e)[:160]

    def run(self, once: bool = False):
        print("buzz-bridge running — channels=%s poll=%ds adapter=%s"
              % (",".join(self.cfg.channels), self.cfg.poll_seconds, self.cfg.adapter.kind))
        while True:
            try:
                self.process_once()
            except Exception as e:  # noqa: BLE001 — daemon must not die
                print("%s round error: %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                              str(e)[:200]),
                      file=sys.stderr, flush=True)
            if once:
                return
            time.sleep(self.cfg.poll_seconds)
