"""Agent adapters — the pluggable layer that turns a prompt into a reply.

Any long-running agent framework or LLM can be wired in by picking an
adapter and giving it options in the config. Adding support for a new
harness means adding one small class here; the bridge core never changes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request

_ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class Adapter:
    def ask(self, prompt: str, sender: str = "", channel: str = "") -> str:  # pragma: no cover - interface
        raise NotImplementedError


class CliAdapter(Adapter):
    """Run an arbitrary command; the prompt is substituted into the argv.

    options:
      command:    list[str], with the literal token "{prompt}" where the
                  prompt should go (e.g. ["hermes", "-q", "{prompt}"]).
      cwd:        working directory (optional).
      timeout:    seconds (default 600).
      strip_ansi: remove terminal colour codes (default true).
      extract:    optional regex with one capture group to pull the reply
                  out of noisy CLI output; if omitted, stdout is used as-is.
      box_agent:  optional name; if set, extracts the text inside a
                  box-drawing frame whose top border contains this name
                  (handles TUI agents like Hermes that print a reply box).
    """

    def __init__(self, options: dict):
        self.command = options["command"]
        self.cwd = options.get("cwd")
        self.timeout = int(options.get("timeout", 600))
        self.strip_ansi = bool(options.get("strip_ansi", True))
        self.extract = options.get("extract")
        self.box_agent = options.get("box_agent")

    def ask(self, prompt: str, sender: str = "", channel: str = "") -> str:
        argv = [prompt if tok == "{prompt}" else tok.replace("{prompt}", prompt)
                for tok in self.command]
        # De afzender gaat mee als omgevingsvariabele zodat een wrapper kan
        # bepalen wat hij mag. Zonder dit kan een agent die tekst van buiten
        # citeert (Hermes vat e-mail samen) een schrijfactie uitlokken.
        omgeving = dict(os.environ, BUZZ_SENDER_PUBKEY=sender or "",
                        BUZZ_CHANNEL_ID=channel or "")
        r = subprocess.run(argv, capture_output=True, text=True, env=omgeving,
                           cwd=self.cwd, timeout=self.timeout)
        out = r.stdout or ""
        if self.strip_ansi:
            out = _ANSI.sub("", out)
        if self.box_agent:
            boxed = _extract_box(out, self.box_agent)
            if boxed:
                return boxed
        if self.extract:
            m = re.search(self.extract, out, re.DOTALL)
            if m:
                return m.group(1).strip()
        return out.strip()


class HttpAdapter(Adapter):
    """POST the prompt to an HTTP endpoint and read the reply from JSON.

    options:
      url:          endpoint.
      method:       default POST.
      headers:      dict (values may reference env via "env:VAR").
      body_template: dict; a value equal to "{prompt}" is replaced with the
                    prompt. Default: {"prompt": "{prompt}"}.
      reply_path:   dot-path into the JSON response (e.g. "choices.0.message.content").
      timeout:      seconds (default 120).
    """

    def __init__(self, options: dict):
        self.url = options["url"]
        self.method = options.get("method", "POST")
        self.headers = _resolve_headers(options.get("headers", {}))
        self.body_template = options.get("body_template", {"prompt": "{prompt}"})
        self.reply_path = options.get("reply_path")
        self.timeout = int(options.get("timeout", 120))

    def ask(self, prompt: str, sender: str = "", channel: str = "") -> str:
        body = _fill(self.body_template, prompt)
        data = json.dumps(body).encode()
        req = urllib.request.Request(self.url, data=data, method=self.method,
                                     headers={"content-type": "application/json",
                                              **self.headers})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.load(resp)
        return _dig(payload, self.reply_path) if self.reply_path else json.dumps(payload)


class AnthropicAdapter(Adapter):
    """Direct Claude call — the simplest way to put an LLM in a channel.

    options: api_key ("env:ANTHROPIC_API_KEY"), model, max_tokens,
             system (optional system prompt).
    """

    def __init__(self, options: dict):
        import os
        key = options.get("api_key", "env:ANTHROPIC_API_KEY")
        self.api_key = os.environ[key[4:]] if key.startswith("env:") else key
        self.model = options.get("model", "claude-haiku-4-5")
        self.max_tokens = int(options.get("max_tokens", 1024))
        self.system = options.get("system")

    def ask(self, prompt: str, sender: str = "", channel: str = "") -> str:
        body = {"model": self.model, "max_tokens": self.max_tokens,
                "messages": [{"role": "user", "content": prompt}]}
        if self.system:
            body["system"] = self.system
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.load(resp)
        return "".join(b.get("text", "") for b in payload.get("content", [])).strip()


_ADAPTERS = {"cli": CliAdapter, "http": HttpAdapter, "anthropic": AnthropicAdapter}


def build_adapter(kind: str, options: dict) -> Adapter:
    if kind not in _ADAPTERS:
        raise ValueError("unknown adapter kind %r (have: %s)"
                         % (kind, ", ".join(sorted(_ADAPTERS))))
    return _ADAPTERS[kind](options)


# ── helpers ──────────────────────────────────────────────────────────────

def _extract_box(text: str, agent_name: str) -> str | None:
    """Pull the reply out of a box-drawing frame whose top border names the agent.

    Agents that stream progress may print several boxes; the last one is the
    final answer, so that is the one returned.
    """
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if agent_name in ln and ("╭" in ln or "┌" in ln):
            start = i + 1
    if start is None:
        return None
    body = []
    for ln in lines[start:]:
        if "╰" in ln or "└" in ln:
            break
        body.append(re.sub(r"^[\s│|]+|[\s│|]+$", "", ln))
    return "\n".join(body).strip() or None


def _resolve_headers(headers: dict) -> dict:
    import os
    out = {}
    for k, v in headers.items():
        out[k] = os.environ.get(v[4:], "") if isinstance(v, str) and v.startswith("env:") else v
    return out


def _fill(template, prompt):
    if isinstance(template, dict):
        return {k: _fill(v, prompt) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, prompt) for v in template]
    if isinstance(template, str):
        return template.replace("{prompt}", prompt)
    return template


def _dig(obj, path: str):
    cur = obj
    for part in path.split("."):
        cur = cur[int(part)] if part.isdigit() else cur[part]
    return cur if isinstance(cur, str) else json.dumps(cur)
