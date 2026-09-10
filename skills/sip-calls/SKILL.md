---
name: sip-calls
description: SIP and VoIP calls through the local Asterisk PBX.
version: 1.1.0
author: DemLabs
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [sip, voip, calls, sales, asterisk]
---

# SIP Calls Skill

## Architecture
Contactor Agent → Local Asterisk (localhost:5060) → SIP Provider → Phone

## Local Asterisk
- Container: asterisk
- SIP port: 5060 (UDP/TCP)
- RTP ports: 10000-20000
- Endpoint: hermes-agent / hermes2024
- Recordings: /opt/asterisk/recordings/

## Making a Call
Use Asterisk AMI or originate via CLI:
```
docker exec asterisk asterisk -rx "channel originate SIP/siptrunk/7913XXXXXXX application Playback hello"
```

## Call Recording
All calls auto-recorded via MixMonitor to /opt/asterisk/recordings/

## Flow for Cold Sales
1. Originate call via Asterisk
2. Wait for answer
3. Play TTS greeting (via SaluteSpeech/Yandex)
4. Listen for response (via STT)
5. Handle objections
6. If interested → update status in SLC, collect info
7. If not interested → log reason, thank politely

## Legal
- Check call time restrictions (9:00-21:00 local time)
- Respect do-not-call lists
- Record consent disclosure
