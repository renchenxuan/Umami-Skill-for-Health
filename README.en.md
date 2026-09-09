# 🏠 Umami · Skill for Health

> **Let Tuntun live in the AI window you already use.**

Umami is a conversational personal health companion built on the [Agent Skills](https://agentskills.io) standard. You do not need to open a separate health app: say “I had beef noodles for lunch” and it records the meal; say “I have eggs and tomatoes in the fridge” and it checks your actual ingredients and preferences before suggesting the next meal; say “What should I do today?” and it turns your real records into one small next step.

This repository is the **Skill edition**. Deterministic data operations run through a dependency-free Python CLI and are stored in local SQLite. The host AI assistant handles natural-language understanding and response composition. It follows the same health-domain principles as the [Umami Web app](https://github.com/renchenxuan/Umami), while making no false claim to include the Web UI, browser board, multi-session runtime, or provider management.

**Skill v0.2 · aligned with Umami Web v4.0**

> 🌐 For the full “Today / Quick record / Fridge / Ask Tuntun” kitchen cockpit, use [Umami Web v4.0](https://github.com/renchenxuan/Umami). For fast, conversation-native logging, install this Skill. Choose either edition by context.

## Why a Skill

The hard part of health management is not knowing what is healthy. It is making the record while life is happening.

Your morning may start with coffee, lunch may happen between meetings, and an evening at the screen may make you forget to move. A dedicated app is easy to postpone. In an AI window you already have open, “I just ate two steamed buns” is a natural record.

Umami keeps the relationship useful and bounded: scripts handle deterministic reads and writes; the model does not invent database facts; recipes read the fridge and preferences first; allergies are hard constraints; nutrition numbers are estimates; symptoms are not diagnosed. It is not a pretend doctor. It is a companion that helps make the next step smaller and more real.

## Relationship to Web v4.0

The Web edition presents the same loop visually:

`Today → Quick record → Next best step → Recent activity → Ask Tuntun`

The Skill edition translates it into conversation:

`stats → choose one action → run the matching script → report a real receipt → decide what is next`

Shared principles:

- First use starts with a local three-step introduction. It never sends an example message automatically.
- Low-risk diet, workout, body, habit, ingredient, shopping, recipe, and goal updates can be completed directly with a clear receipt.
- Clearing data, changing health preferences, and archiving existing records require confirmation.
- Nutrition and freshness are estimates. User records and AI inferences stay distinct.
- Local structured reads do not implicitly send data just because a summary is being loaded.

The Skill does not include the Web visual UI, drag-and-drop board, browser navigation, Web conversations, or Web provider settings. The conversation window is its entry point.

## Features

- 🧭 **Today loop**: `stats` returns today’s meals, workout minutes, habits, latest weight, expiring ingredients, goals, shopping items, recent activity, and next steps
- 🧊 **Fridge management**: add, update, archive, and filter by fridge/freezer with freshness estimates
- 📸 **Photo recognition workflow**: show recognized ingredients first, then save only after confirmation
- 🍳 **Recipes and shopping**: read the fridge and preferences first; prioritize expiring ingredients; enforce allergies
- 🍚 **Diet logging**: infer meal type and store structured meals and snacks
- 📊 **Nutrition estimates**: calories and macros with serving assumptions and uncertainty
- 🏋️ **Fitness and body data**: workout duration, weight, body fat, and real trend discussion
- 🎯 **Goals and habits**: track goals plus sleep, hydration, and everyday habits
- 🧰 **Recoverable data package**: JSON export, validation-only preview, merge import, and an automatic SQLite backup
- 🛡️ **Health safety**: no diagnosis, prescriptions, or medication changes; urgent warning signs take priority

## Privacy boundary

- `health_db.py` uses only the Python standard library, reads and writes the user-selected local SQLite file, and does not call external APIs.
- The local database and exported package do not contain model keys, map keys, or system credentials.
- The Skill runs inside a host AI conversation. Whether text, images, or context are sent to a cloud model is determined by the host assistant, provider, configuration, and privacy policy—not by this script.
- For sensitive health content, use the host’s AI consent/privacy controls. If the host does not provide a clear mechanism, ask for explicit permission first.

“Local-first” describes the storage and data-operation boundary of the Skill. It does not mean the host model can never see the conversation.

## Quick start

Requires Python 3.10+. Standard library only; no `pip install` is needed.

### Install

**Windows PowerShell:**

```powershell
.\install.ps1
```

**macOS / Linux:**

```bash
./install.sh
```

Restart your AI assistant so it can rediscover the `umami-health` Skill.

### First use

On the first health-data operation, the assistant runs the idempotent initializer:

```bash
python "<skill-directory>/scripts/health_db.py" init
```

When the result contains `first_run: true`, Tuntun gives a three-step local introduction: meet Tuntun, record one small thing, and let Umami organize the rest. It does not send an automatic message or silently call a model.

### Data location

| Priority | Location |
|---|---|
| 1 | CLI `--db` argument |
| 2 | `UMAMI_HEALTH_DB` environment variable |
| 3 | `~/.umami/health.db` |

## Common commands

In the examples below, `<py>` means:

```bash
python "<skill-directory>/scripts/health_db.py"
```

| Goal | Command |
|---|---|
| Today overview | `<py> stats` |
| Fridge | `<py> ingredients list` / `add` / `update` / `archive` |
| Diet | `<py> diet log --meal 午餐 --foods '米饭,清蒸鲈鱼'` |
| Workout | `<py> workouts log --activity 慢跑 --duration 30` |
| Body | `<py> body log --weight 72.5 --fat 20.1` |
| Goals | `<py> goals set --name 减脂 --target 减到65kg` |
| Habits | `<py> habits log --habit 睡眠 --value 睡了7小时` |
| Shopping | `<py> shopping add --name 西兰花` / `check <id>` |
| Preferences | `<py> prefs get` / `set` |
| Recipes | `<py> recipes save` / `list` / `show <id>` |
| Food catalog | `<py> foods search 番茄` / `categories` |
| Export | `<py> export --output umami-health.json` |
| Preview import | `<py> import-preview --file umami-health.json` |
| Confirm import | `<py> import --file umami-health.json --yes` |

`stats.metrics.diet_kcal` and `calorie_target` are currently `null` in the Skill data model. The Skill does not invent calories just to fill a dashboard field. Report nutrition only when an explicit estimate exists and follow the nutrition reference rules.

## Write policy

Low-risk logs—diet, workouts, body metrics, habits, ingredients, shopping items, recipes, and goal status—can be completed directly. After a successful command, the assistant must say clearly that the item was recorded or saved.

Confirmation is required before clearing the fridge or shopping list, changing people count/taste/allergies/height/age and other health preferences, or archiving existing records.

The Skill currently provides script receipts and safe archiving. It does not provide the Web edition’s per-action undo button; do not describe an archive or database backup as an instant undo.

## Recoverable health package

```bash
<py> export --output umami-health.json
<py> import-preview --file umami-health.json
<py> import --file umami-health.json --yes
```

The package contains preferences, ingredients, diet logs, workouts, body metrics, goals, habits, recipes, and shopping items managed by this Skill. Import validates the entire package first; preview never writes. Confirmed import creates a backup before merging and does not overwrite existing records by default. IDs are preserved where possible and remapped on conflicts. Web conversations, reminders, schedules, and board state are not Skill tables and are not falsely included.

## Project structure

```text
├── install.ps1 / install.sh
├── README.md                  # Chinese guide
├── README.en.md               # English guide
├── examples/test-prompts.md   # Workflow and regression prompts
├── tools/export-foods.ts      # Read-only food seed exporter
└── skills/umami-health/
    ├── SKILL.md               # Persona, boundaries, commands, workflows
    ├── references/             # Safety, recipe, fitness, diet, nutrition rules
    └── scripts/
        ├── health_db.py       # Local SQLite data CLI
        └── seed_foods.json     # 165 common ingredients
```

## Product translation

| Web v4.0 capability | Skill implementation |
|---|---|
| Today page | `stats` metrics, next steps, and recent activity |
| Quick record | Diet, workout, body, and habit commands |
| Ask Tuntun | Host AI conversation plus `SKILL.md` output rules |
| First-run onboarding | `init` returns `first_run` and the assistant presents a static guide |
| AI consent | Web owns the app-level gate; the Skill relies on host AI privacy controls and does not fake a gate the script cannot enforce |
| Automatic receipt | Successful JSON output plus a clear assistant reply; no fake Web action undo |
| Recoverable export | `export`, `import-preview`, and confirmed `import` for Skill-local data |

The architecture remains simple: deterministic operations go through the bundled script; the model interprets, reasons, and communicates; domain references load progressively.

## Status

This is an actively refined personal project. The Skill edition now follows Umami Web v4.0’s daily loop and trusted data boundary, while host AI networking, vision, and model consent remain dependent on the assistant you use. Issues and pull requests are welcome.

## License

MIT License
