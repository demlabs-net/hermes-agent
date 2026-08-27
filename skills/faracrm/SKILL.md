---
name: faracrm
description: FaraCRM data model and CRM tools usage. Explains the difference between company (компания), partner (контактное лицо), contact (средство связи) and contact_type (тип контакта), leads and their funnel stages. Use before creating or updating CRM records.
version: 1.0.0
author: DemLabs
platforms: [linux]
metadata:
  hermes:
    tags: [faracrm, crm, sales, leads, partners, contacts, database, demlabs]
---

# FaraCRM Skill — модель данных и правила работы

## Когда использовать
Перед ЛЮБЫМ обращением к FaraCRM MCP: перед созданием компании, контакта, лида,
задачи или заметки. Помогает не путать сущности и выбрать правильный инструмент.

## Ключевое: 4 разные сущности, которые часто путают

| Сущность | Что это | Поля-маркеры | Где живут |
|----------|---------|--------------|-----------|
| **company** | Компания (юрлицо/организация) | name, inn, kpp, ogrn, okpo, chief_id, accountant_id | `faracrm_search/create/get/update_company` |
| **partners** | Контактное лицо (человек) | name (ФИО), company_id, website, notes, contact_ids | `faracrm_search/create/get/update_partner`, `faracrm_add_note` |
| **contact** | Средство связи (телефон, email, мессенджер) | name (значение), partner_id, contact_type_id, is_primary | Внутри `partners.contact_ids`, НЕ отдельный MCP-инструмент |
| **contact_type** | Тип средства связи (справочник) | id, name | 1:phone, 2:email, 3:telegram, 4:whatsapp, 5:viber, 6:instagram, 7:vk, 8:avito, 9:website, 10:web_push, 11:max_bot, 12:max |

**Запомни:**
- «Контакт» в разговоре = **partners** (человек), а не contact!
- «Тип контакта» (contact_type) — это ТЕЛЕФОН/EMAIL/TELEGRAM и т.п., а НЕ должность
  и НЕ тип человека. Типа «юридическое лицо» в contact_type нет.
- Телефоны и email человека хранятся в `partners.contact_ids` (таблица contact).
  **В ответе `faracrm_search_partners` полей phone/email НЕТ** — только contact_ids
  (может быть пустым в списке; полный список — через `faracrm_get_partner`).
- **Компания и контактное лицо — разные записи.** У компании есть ИНН/КПП/ОГРН,
  у человека — только ФИО, телефон, email. Связь: `partners.company_id` → company.

## Лид (lead) — это сделка, а не контакт

Лид — потенциальная продажа, движется по воронке. НЕ путать с партнёром!

| Поле | Смысл |
|------|-------|
| name | Название лида/сделки |
| type | `lead` или `opportunity` |
| stage_id | Стадия воронки (см. ниже) |
| partner_id | Связанное контактное лицо (может отсутствовать) |
| company_id | Связанная компания (может отсутствовать) |
| notes | Заметки по сделке |
| user_id | Ответственный менеджер |
| team_id | Команда |

**Стадии воронки (lead_stage):**
1. Новый → 2. Квалификация → 3. Предложение → 4. Переговоры → 5. Выиграно / 6. Проиграно
(точные ID получай через `faracrm_get_lead_stages` — они стабильны, но проверяй).

**Правила работы с лидами:**
- `faracrm_create_lead` ОБЯЗАТЕЛЬНО требует `stage_id` (сначала получи через
  `faracrm_get_lead_stages`).
- Перемещение по воронке = `faracrm_update_lead` c `{"stage_id": <номер>}`.
- Заметки по сделке — поле `notes` лида (через update_lead), а не faracrm_add_note.

## Заметки: куда какая

| Инструмент | К чему пишет | Когда |
|------------|--------------|-------|
| `faracrm_add_note(partner_id, note_text)` | В поле `notes` контактного лица | После разговора с человеком |
| `faracrm_update_lead(fields: {"notes": "..."})` | В поле `notes` лида | Комментарий по сделке |
| `faracrm_update_company(fields: {...})` | Поля компании | ⚠️ у компании НЕТ поля `notes` — только name, inn, kpp, ogrn, okpo и т.п. |

## Активности и задачи — не одно и то же

- **activity** (история взаимодействий, `faracrm_get_activity`): звонки/встречи/email
  (activity_type: 1 Звонок, 2 Встреча, 3 Email, 4 Напоминание, 5 Задача). Привязка —
  через generic relation `res_model` + `res_id` (например res_model="partners",
  res_id=<partner_id>). Для истории по человеку всегда указывай partner_id —
  инструмент сам построит правильный фильтр.
- **task** (`faracrm_create_task`, `faracrm_search_tasks`): задача менеджеру.
  Требует `project_id`. Связи с контактным лицом НЕТ (только проект/исполнитель).

## Типовые сценарии

### 1. Нашёл компанию → добавил контактное лицо
```
faracrm_search_companies("ООО Ромашка") → id=10
faracrm_create_partner(name="Иванов Иван", company_id? — НЕТ такого параметра!)
```
⚠️ `faracrm_create_partner` НЕ принимает company_id. Порядок:
1. `faracrm_create_partner(name="Иванов Иван")` → id нового контакта
2. `faracrm_update_partner(partner_id, '{"company_id": 10}')` — привязать к компании
   (или notes/website).

### 2. Звонок новому лиду
```
faracrm_get_lead_stages() → берём id стадии «Новый»
faracrm_create_partner(...) → контактное лицо (если нет)
faracrm_create_lead(name="ООО Ромашка — лид", stage_id=<id>, partner_id=<id>)
faracrm_add_note(partner_id, "Перезвонил, договорились о встрече")
faracrm_create_task(name="Отправить КП", project_id=<id>)
faracrm_update_lead(lead_id, '{"stage_id": <id Квалификация>}')
```

### 3. Входящий звонок (secretary)
```
faracrm_search_partners(query=<имя из трубки>) → есть ли такой человек
если нет → faracrm_create_partner(name=...)
faracrm_get_partner(<id>) → contact_ids, notes (история контакта)
faracrm_add_note(<id>, "Входящий звонок: ...") → зафиксировать разговор
faracrm_search_leads(stage_id=0) → проверить активные лиды по этому клиенту
```

## Чек-лист перед записью в CRM
1. Компания — юрлицо? → инструменты company (ИНН/КПП/ОГРН туда).
2. Человек? → инструменты partner (ФИО, notes, website). Телефон/email — только
   как contact (contact_ids), в MCP пока не пишутся.
3. Сделка/воронка? → инструменты lead + stage_id из faracrm_get_lead_stages.
4. Действие на будущее (перезвонить, КП)? → faracrm_create_task (нужен project_id).
5. История взаимодействий? → faracrm_get_activity(partner_id).
6. Разговор состоялся → заметка: faracrm_add_note (человек) или notes лида (сделка).

## Не путать: быстрые ответы
- «Куда записать ИНН?» → company (inn/kpp/ogrn).
- «Куда записать телефон?» → contact (contact_ids партнёра), НЕ в partners.name и НЕ в company.
- «Тип контакта» → это phone/email/telegram (contact_type), не должность.
- «Лид или контакт?» → «Есть воронка/стадия?» → лид. «Это человек?» → partner.
- «Заметку куда?» → после звонка человеку: faracrm_add_note. По сделке: notes лида.
