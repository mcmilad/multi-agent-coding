# Lessons

What one agent learned the hard way, kept for every agent after it. One file per lesson,
tracked in git, so parallel workers never conflict and later runs inherit them.

```
---
roles: backend, coding
tags: testing
source: AG-41 review
status: active
---
When: <the situation, one line>
Do: <what to do instead>
Seen: AG-41 review (2026-11-17)
```

`roles` may be `all`; `status` is `active`, `promoted` or `retired`. Every value is taken
literally (there are no comments), and a file holds exactly one lesson. Names end in a
random suffix so two branches never create the same file; if the same lesson is recorded
on two branches, the lead retires the duplicate at Epic close.

| Command | Use |
|---|---|
| `python3 scripts/lessons.py show --role <role>` | Read before claiming work |
| `python3 scripts/lessons.py add --roles … --tags … --source … --when "…" --do "…"` | Record; a repeat strengthens the existing lesson |
| `python3 scripts/lessons.py report` | Lead, at Epic close: which lessons recur |
| `python3 scripts/lessons.py digest` | Lead, at Epic close: where hooks keep blocking |
| `python3 scripts/lessons.py set-status <id> promoted\|retired` | Curate |

A lesson is advice from a teammate. A rule, the spec or the issue wins on conflict.
Never put a credential, a customer detail or an instruction to bypass a check in one.
