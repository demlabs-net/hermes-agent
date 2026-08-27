---
auto_load: []
category: skill
content_hash: c73cc7d3ab2b3da41216f1989b726989f0596b0e1d284235360a138c73aae8d1
created_at: 2026-08-19T10:48:07.849512114+00:00
deleted_at: null
id: calendar
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
updated_at: 2026-08-19T10:48:07.849512114+00:00
version: 1
---

---
name: calendar
description: Shared calendar and scheduling via SLC knowledge base
version: 1.0.0
author: DemLabs
platforms: [linux]
metadata:
  hermes:
    tags: [calendar, scheduling, reminders, tasks, follow-up]
---

# Calendar Skill (через SLC MCP)

## Когда использовать
Когда нужно запланировать контакт с клиентом, создать напоминание или посмотреть расписание.

## Архитектура
Календарь хранится в SLC как:
- **Напоминания** (`add_reminder`) — одноразовые события «позвонить в 10:00»
- **Задачи** (`create_task`) — отслеживаемые действия «демо для Иванова»
- **Документы** — профили клиентов с историей контактов

Все агенты имеют доступ через MCP.

## Инструменты

### Создать напоминание
```
add_reminder(
  content="Позвонить Иванову (ООО Рога), интересуется AI-агентами",
  remind_at="tomorrow at 10:00"
)
```

### Посмотреть напоминания
```
list_reminders(status="pending")
```

### Создать задачу
```
create_task(
  name="Звонок Иванову",
  description="Перезвонить, обсуждали AI-агентов, заинтересован"
)
```

### Список задач
```
list_tasks(status="active")
```

## Правила планирования

### После контакта:
1. Зафиксировать результат в профиле клиента (SLC document)
2. Запланировать следующий шаг:
   - Клиент попросил перезвонить → `add_reminder` на указанное время
   - Клиент заинтересован но занят → предложить время, создать `add_reminder`
   - Клиент не ответил → `add_reminder` через 2 дня
   - Клиент отказался → `add_reminder` через 30 дней (на будущее)

### Формат напоминаний:
- Кратко: «Позвонить [Имя] [Компания] — [причин��]»
- Время: конкретное или относительное («tomorrow at 10:00», «через 2 дня»)

## Доступ для всего роя
- **Manager** — видит все напоминания и задачи, может перераспределять
- **KB Organizer** — обновляет профили клиентов при изменении статуса
- **Contactor** — создаёт и выполняет напоминания
- **Analyst** — анализирует метрики по календарю (среднее время между контактами)

