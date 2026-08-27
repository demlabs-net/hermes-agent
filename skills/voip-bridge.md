---
auto_load: []
category: skill
content_hash: ee91c79198282cdf74a6fddf3f75c528aabe3dd513f82a9ff7ec4dd15d72534c
created_at: 2026-08-19T10:48:05.277803457+00:00
deleted_at: null
id: voip-bridge
metadata:
  archived: null
  compression_batch_id: null
  consolidated: null
  date: null
  doc_level: null
  doc_type: null
  importance: null
  seat_id: null
  source: null
  source_count: null
references: []
seat_id: manager
tags: []
updated_at: 2026-08-19T10:48:05.277803457+00:00
version: 1
---

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
- `listen(timeout_s)` returns the **next speech event** from the caller, chunk
  by chunk: `{type, text}` where type is one of:
  - `speech_started` — the caller began talking (text empty)
  - `partial` — progressive recognition of the current utterance (text grows
    with each event; react to it only if the meaning is already clear)
  - `final` — the complete utterance (text) — **this is the unit you reply to**
  - `speech_ended` — caller stopped (text empty)
  - `endpoint` — VAD end-of-turn (text empty)
  - `timeout` — nobody spoke within `timeout_s` (text empty)
- Loop: `listen()` repeatedly; on `final` — compose your reply and `speak()` it.
  On `timeout` — the caller is silent: decide whether to ask again, wait more
  (call `listen()` again) or end the call.
- **Barge-in**: if the caller starts speaking while you talk, your speech is
  cut immediately (you will get a `speech_started` event) — shut up and listen.
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

