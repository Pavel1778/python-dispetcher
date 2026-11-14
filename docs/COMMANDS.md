# Chat commands and hashtags

NEXUS reads a chat message as a request *and* as a set of instructions to
itself. Slash commands choose how a turn runs, hashtags say what the work is
about and what it may not do, and typed parameters carry a value.

The whole vocabulary lives in `orchestration/nexus_commands.py`. There is one
parser, used by the chat turn and by every interface button, so a button and a
slash command can never drift apart: a button posts `{"action": "/plan"}` and
the server runs it through the same code.

## Commands

Two kinds. A **mode** command chooses how the turn runs; a **task** command
names the kind of work. One of each may be combined.

| Command | Kind | Effect |
| --- | --- | --- |
| `/chat` | mode | Answer in words (mode `ask`). Nothing runs. |
| `/plan` | mode | Produce a plan with dependencies. Nothing runs. |
| `/autorun` | mode | Start an autonomous run. |
| `/audit` | task | Read-only audit (mode `analysis`). |
| `/fix` | task | Fix a defect (mode `autonomous`). |
| `/test` | task | Write or repair tests. |
| `/review` | task | Review changes, read-only. |
| `/deploy` | task | Deploy. |
| `/research` | task | Investigate and report, read-only. |
| `/status` | task | Report the project's state, read-only. |
| `/help` | meta | List the vocabulary. |

Aliases resolve to the canonical command (`/вопрос` → `/chat`, `/auto` →
`/autorun`, `/план` → `/plan`, …). `/help` answers with the vocabulary and asks
no model.

## Hashtags

Four groups. They travel with the request into the task text the planner and
the agents read.

- **technology** — `#frontend` `#backend` `#database` `#api` `#infrastructure`
  `#devops` `#mobile` `#desktop`
- **quality** — `#testing` `#security` `#performance` `#refactoring`
  `#documentation` `#accessibility`
- **execution** — `#parallel` `#sequential` `#deep` `#fast` `#critical`
- **restriction** — `#no-merge` `#no-deploy` `#no-delete` `#read-only`

Restrictions have priority over commands:

- `#read-only` beside an execute command (`/autorun`, `/fix`) downgrades the
  turn to a read-only analysis and reports the downgrade. The turn still runs;
  it simply does not change anything.
- A restriction no downgrade can satisfy is refused: `/deploy #no-deploy` is a
  400, because deploying without deploying is not a thing.
- `#no-merge` beside `/deploy` keeps the deploy and warns that no merge will be
  performed.

## Parameters

Written `--name=value` or `--name value`. A bare `--name` is a flag only for a
boolean parameter.

| Parameter | Type | Commands |
| --- | --- | --- |
| `--repo` | string | `/audit` `/fix` `/test` `/review` `/deploy` `/research` |
| `--branch` | string | the same |

An unknown parameter or a wrong type is a 400 with the reason.

## Examples

```
/plan #backend #security починить вход в систему
/autorun #frontend #mobile #testing починить форму входа
/audit #deep #security
/fix #critical #backend #testing --repo owner/name исправить падение
/plan /audit #database
/plan #read-only перепиши модуль
```

## What the parser keeps

Only tokens that name a real command or tag are removed from the request. A
path (`/tmp/file.txt`), an issue number (`#1234`) and any other word are left
exactly where they were written, so a pasted specification is preserved byte
for byte apart from the instructions NEXUS understood.

An unknown command (`/nosuchcommand`) is a 400 before anything is written; an
unknown tag is reported in `unknown_tags` and left in the request.

## The interface

`GET /api/commands` returns the whole vocabulary as JSON (commands, tags,
groups, help text); `GET /api/commands?q=/au` adds completions. The studio
builds its command menu from this, so the menu is always the server's own
vocabulary. Every button that starts work posts its `action` through the same
parser as a typed command.

## Where it is enforced

- `orchestration/nexus_commands.py` — the parser, the registry, suggestions,
  the help text, `metadata()` and `task_text()`.
- `orchestration/web_server.py` — `_chat_turn` parses before the Intent Router;
  `GET /api/commands`.
- `tests/test_commands.py` — the pure parser and the observable HTTP behaviour.
