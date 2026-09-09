---
auto_load: []
category: skill
content_hash: 4fcb650aa1678f7f6144f009c118e18cd0b4cd5797ce153f3d4e9e694b7bb033
created_at: 2026-08-19T10:48:00.636759188+00:00
deleted_at: null
id: max-messenger
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
updated_at: 2026-08-19T10:48:00.636759188+00:00
version: 1
---

---
name: max-messenger
description: Integration with MAX (Макс) Russian messenger for sales outreach
version: 1.0.0
author: DemLabs
platforms: [linux]
metadata:
  hermes:
    tags: [max, messenger, russian, bot, sales]
---

# MAX Messenger Skill

## When to Use
Use this skill to send messages via MAX (Макс) messenger bot API.

## MAX Bot API
- Documentation: https://dev.max.ru/docs
- JavaScript SDK: https://dev.max.ru/docs/chatbots/bots-coding/js
- Golang SDK: https://dev.max.ru/docs/chatbots/bots-coding/go

## Setup Requirements
1. Register on https://business.max.ru
2. Create organization profile
3. Create bot via platform
4. Get bot token
5. Set MAX_BOT_TOKEN in .env

## API Endpoints
- Base URL: https://bot-api.max.ru
- Send message: POST /messages
- Get updates: GET /updates

## Message Types
- Text messages
- Rich text (HTML)
- Buttons (inline keyboard)
- Media (images, files)

## Sales Templates
Use personalized templates for cold outreach.
