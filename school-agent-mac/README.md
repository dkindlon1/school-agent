# School Agent — macOS

A study assistant that only cares about one thing: that you get good grades.

It syncs every class's deadlines from your school's calendar, tracks what
each assignment is actually worth, and — when you sit down to work — puts a
tutor in front of you that knows your course material. It runs entirely on
your own Mac.

> **macOS version.** There is a separate, standalone Windows project at
> [school-agent-windows](https://github.com/dkindlon1/school-agent-windows).
> The two are independent — neither depends on the other, and neither needs
> the other to be installed.

## Getting started

**Double-click `Open School Agent.command`.**

The first run does the whole install: it finds a usable Python (or installs
one via Homebrew if you don't have 3.10+), builds a private environment for
the app, installs its libraries, writes a starter `.env`, and launches.
Terminal opens, then your browser follows.

Every run after that is the same double-click and takes a couple of seconds.

Leave the Terminal window open while you use the app — that window *is* the
app, and it's also what syncs your deadlines in the background every 30
minutes. Closing it stops the app.

**Want it in your Dock?** Drag `Open School Agent.command` onto the right-hand
side of the Dock, or into your Applications folder.

You'll land on a dashboard with an **Add a class** panel. Full instructions
for everything — pulling calendar feeds out of Brightspace, grade tracking,
the study modes, backups, troubleshooting — live inside the app under
**Settings → How to use this**.

### The two macOS things that will trip you up

**1. Gatekeeper.** If you downloaded this as a zip, macOS quarantines it and
double-clicking gives you *"cannot be opened because it is from an
unidentified developer."* Fix it once, either way:

- **Right-click** `Open School Agent.command` → **Open** → **Open**. Or:
- In Terminal, from this folder: `xattr -dr com.apple.quarantine .`

Cloning with `git` instead of downloading a zip avoids this entirely.

**2. Notification permission.** The first time the app posts a deadline
reminder, macOS asks whether to allow notifications for Terminal. If you say
no, banners stop appearing — silently, with no error. Re-enable it under
**System Settings → Notifications → Terminal**. Everything still shows up in
the app and the Terminal window regardless.

### If `Open School Agent.command` won't run

The executable bit can get lost when a file is copied through some tools.
From this folder in Terminal:

```
chmod +x "Open School Agent.command" start.sh setup.sh
```

You can also skip the double-click entirely and run `./start.sh`.

### Timezone

macOS reports its timezone properly, so the app picks it up on its own. If
your Mac's clock is set to a different zone than you're actually in, set it
explicitly in `.env`:

```
SCHOOL_AGENT_TIMEZONE=America/New_York
```

This decides what counts as "due today" versus "overdue".

## What it does

- **Deadlines, synced.** Reads your school's calendar feed (Brightspace/D2L
  and anything else that exports ICS) every 30 minutes while it's running.
  Recurring assignments expand properly; cancelled ones disappear; anything
  the calendar never marks off can be cleared by hand so it stops nagging.
  Each item links straight to the assignment, not just to a calendar entry.
- **Grades that mean something.** Point it at your syllabus and it reads the
  grading table — weights, item counts, drop-lowest, exam dates. From then on
  it can answer the only question that matters: *what do I need on what's
  left to land the grade I want*, and *which of these three things due this
  week is actually worth doing first*.
- **Seven ways to study**, not one. Flashcards on an FSRS spaced-repetition
  schedule, worked examples, guided practice with hints you unlock one at a
  time, explain-it-back (you write it, it grades you), why-questions, concept
  maps, and interleaved drills. It suggests one based on your real state —
  what you've studied, what you keep failing, how close the exam is.
- **Struggle ladders.** Say what keeps costing you marks, in your own words,
  and it builds a run of practice problems whose support fades one rung at a
  time — fully worked, then you finish it, then you do the hard part, then
  just the principle, then on your own. Getting one wrong drops you back a
  rung. That's the feature.
- **A daily briefing** that scans everything and tells you where you stand,
  ordered by grade impact rather than by whichever date happens to be first.
- **Focus mode** — one thing at a time, everything else hidden, with a timer.
- **A chat** that can see your uploaded material, with `@`-mentions for
  classes and documents.

## What it needs

- Python 3.10 or newer — setup installs it if you don't have it.
- A calendar feed URL from your school, if you want deadline sync. In
  Brightspace: **Calendar → Subscribe**. Nothing else works without it, and
  everything else works without it.
- Optionally, an API key for OpenAI, Claude, or Gemini — or a local
  [Ollama](https://ollama.com) install, which is free and private. Deadlines,
  documents, grade tracking and flashcard review all work with no model at
  all. Generation (study modes, ladders, chat, briefings) needs one.

Paste a key on the **Settings → Model & keys** tab, or put it in `.env`. If a
cloud provider is busy or rate-limited it retries, then falls back to a local
Ollama if you have one running.

## Your data stays yours

Everything lives in plain files in this folder — `data/` for your coursework,
`config/classes.yaml` for your classes, `.env` for your keys. Nothing is
uploaded anywhere except the text of whatever you ask a model about, and only
to the provider you configured. All three are in `.gitignore`, so a `git
push` from this folder cannot publish your feed tokens, your API key, or a
single one of your grades.

To back it up, copy the folder. To move machines, copy the folder.

## How it's built

- One Flask server and one HTML file. No build step, no bundler, no database.
- `src/school_agent/` is a plain Python library with no framework in it — the
  UI is a client of the library, never the other way round.
- Model access goes through one function with one shape,
  `llm_fn(prompt, context) -> str`. No feature knows which provider is
  configured, which is why adding one was a contained change.
- Every JSON write is atomic and every read self-heals: a file corrupted by a
  crash or a full disk quarantines itself and the app keeps running rather
  than dying on startup forever.
- The library never invents your course facts. Deadlines, weights and exam
  dates come only from what you gave it. Subject knowledge is the model's own
  and is used freely — an uploaded PDF is context that tunes the answer to
  your course, not permission to know what a vector is.

## Layout

```
Open School Agent.command    double-click this
setup.sh                     first-run installer, called automatically
.env                         model provider config (created from .env.example)
config/classes.yaml          your classes — the dashboard writes this
data/<class-slug>/           materials, deadlines, cards, grades, ladders
src/school_agent/            the library
  storage.py                   atomic writes + self-healing loads
  llm.py                       the one place a provider is chosen
  localtime.py                 one definition of "what day is it"
  study.py                     study modes and the heuristic that picks one
  ladder.py                    struggle ladders
ui/                          the dashboard (Flask + one HTML file)
tests/                       no network, no fixtures to maintain: `pytest`
```

## Running the tests

```
pytest
```

394 of them, covering the library and every dashboard API route. No network
access, so they run anywhere. A good few are named after bugs that actually
shipped — the comments say what each looked like from a student's side,
because that's the part worth not repeating.

## License

MIT — see LICENSE.
