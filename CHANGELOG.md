# Changelog

## 0.4.1

- Ook het kanaal gaat mee naar de adapter: `Adapter.ask(prompt, sender, channel)`,
  als `BUZZ_CHANNEL_ID` in de omgeving van een `CliAdapter`-commando. Met meerdere
  kanalen moet een agent weten waar een bericht vandaan komt — anders beantwoordt hij
  een vraag over het ene onderwerp met de context van het andere.
- Adapters met alleen `ask(prompt)` of `ask(prompt, sender)` blijven werken.

## 0.4.0

- De afzender van een bericht gaat mee naar de adapter: `Adapter.ask(prompt, sender)`,
  en `CliAdapter` zet hem als `BUZZ_SENDER_PUBKEY` in de omgeving van het commando.
  Zo kan een wrapper onderscheiden wie iets vraagt — nodig zodra een agent meer mag
  dan lezen, want een agent die tekst van buiten citeert (bijvoorbeeld een
  e-mailsamenvatting) mag geen schrijfactie kunnen uitlokken.
- Adapters van buiten deze repo die alleen `ask(prompt)` kennen blijven werken; de
  bridge kijkt naar de signatuur voordat hij de afzender meegeeft.

## 0.3.2
- **Fix:** replies starting with `-` (bullet lists) were parsed as CLI flags
  and silently never posted; content is now passed dash-safe (`--content=`).

## 0.3.1
- **Fix:** replying under a message that is itself a thread reply now targets
  the thread *root* (relays reject replies-to-replies).
- Send failures are logged (`SEND-FAILED` + relay error) instead of swallowed.

## 0.3.0
- `context_messages`: prepend the last N channel messages to the prompt
  (labeled via `display_names`) so follow-up questions work.
- `thread_replies`: answer as a thread reply under the mention.
- Rich @-mentions (p-tag matching `self_pubkey`) always trigger, independent
  of display names or regex.
- Timestamped log lines; successful answers are logged.

## 0.2.2
- `silence_token`: an agent reply consisting of exactly this token posts
  nothing — lets the agent decline messages that merely quote its name.

## 0.2.1
- **Fix:** state is persisted *before* the agent runs, so a crash or restart
  during a slow answer can never double-post (at-most-once).
- **Fix:** box extraction takes the *last* box-drawing frame, so streaming
  agents get their final answer posted instead of an intermediate line.

## 0.2.0
- Agent-to-agent replies: with `agent_pubkeys` set, agents may mention each
  other (task relays), capped by `max_agent_chain` consecutive agent-authored
  messages — a human message resets the chain, so loops are impossible.

## 0.1.0
- Initial release: cli / http / anthropic adapters, mention matching,
  multi-channel, idempotent state, `env:`/`file:` secret indirection.
