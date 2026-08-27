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
