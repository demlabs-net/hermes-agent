---
name: voip-bridge
description: Voice call control via voip-bridge MCP server
version: 1.2.0
author: DemLabs
---

# VoIP Bridge

MCP server at `http://zeus.local:3002/mcp` (Streamable HTTP transport, `rmcp`)

## Tools

| Tool | Description |
|------|-------------|
| `make_call(phone_number, profile_id, voice_id, greeting)` | Make outbound call. Returns call_id. Wait for `media_connected` webhook before speak/listen |
| `answer_call()` | Answer incoming call. Use after `incoming_call` webhook |
| `listen(call_id, timeout_s, min_speech_ms)` | Returns the NEXT caller speech event `{type, text}` (speech_started/partial/final/speech_ended/endpoint/timeout) — stream the conversation by calling it repeatedly |
| `speak(call_id, text)` | Speak text to caller (TTS) — **returns immediately**, audio streams out as it is generated. Use TTS tags for natural speech |
| `end_call(call_id)` | End (hang up) a call |
| `get_call_status(call_id)` | Check call status |
| `list_active_calls()` | List all active calls |
| `get_tts_tags()` | Get TTS tags (breathing, pauses, emotion) |
| `list_profiles()` | List voice profiles |

## Conversation loop (streaming, BOTH ways)

- `speak()` is **fire-and-forget**: it queues the text and returns at once — the
  TTS element synthesizes in chunks and streams them out while generating. Do
  NOT sleep after `speak()`; just call `listen()` right away.
- `listen(timeout_s)` returns the **next meaningful speech event**: `{type,
  text}` where type is one of:
  - `final` — the complete utterance (text) — **this is the unit you reply to**
  - `partial` — long progressive recognition (text ≥ ~10 chars; react only
    if the meaning is already clear — the final is coming)
  - `timeout` — nobody spoke within `timeout_s` (text empty)
  - (speech_started / speech_ended / endpoint markers and short fragments
    are **hidden** — the bridge skips them so you react once per utterance,
    not once per event)
- Loop: `listen()` repeatedly; on `final` — compose your reply and `speak()` it.
  On `timeout` — the caller is silent: ask again or wait more (call `listen()`
  again), NEVER end the call on timeout.
- **Barge-in**: if the caller starts speaking while you talk, your speech is
  cut immediately (you will get a `speech_started` event) — shut up and listen.
- **Ending a call**: hang up ONLY when the caller ends the conversation
  (they said goodbye / finished), or the `call_ended` webhook arrives (the
  caller already hung up or the line dropped). Never hang up first on
  inbound calls — silence/timeout/empty recognition is NOT a reason to end.
  For outbound (cold sales) use the same rule: let the prospect finish.
- Keep replies short — the first audio of a reply arrives in seconds; a long
  monologue gets cut at the configured limit.

## Webhook events
Events arrive through the Hermes webhook system:
- `incoming_call` — new inbound call detected, use `answer_call()`
- `call_answered` — callee answered (outbound) or bridge answered (inbound)
- `media_connected` — WebRTC audio ready, can use `speak()`/`listen()`
- `call_ended` — call terminated

## TTS Tags
Always call `get_tts_tags()` before generating speech text.
The MCP server provides the authoritative tag list.

## Profiles
- `secretary` — female voice (Анна), Fish Audio ID `2a1036d6...`
- `contactor` — male voice (Алексей), Fish Audio ID `4fc46239...`

Set via `profile_id` in `make_call()`.
