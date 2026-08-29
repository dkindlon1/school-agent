# School Agent — Windows

A study assistant that only cares about one thing: that you get good grades.

It syncs every class's deadlines from your school's calendar, tracks what
each assignment is actually worth, and — when you sit down to work — puts a
tutor in front of you that knows your course material. It runs entirely on
your own machine.

> **Windows version.** There is a separate, standalone macOS project at
> [school-agent-mac](https://github.com/dkindlon1/school-agent-mac). The two
> are independent — neither depends on the other, and neither needs the other
> to be installed.

## Getting started

**Double-click `setup.bat`.**

That's the whole install. It checks your machine for everything the app needs
and installs whatever is missing — *including Python itself* if you don't
have it, as a per-user install with no administrator password. Then it builds
a private environment for the app, installs its libraries, writes a starter
`.env`, and launches. One run, start to finish.

After that, **`start.bat`** is the everyday entry point. Double-click it; a
console window opens and your browser follows. Leave the console window open
while you use the app — that window *is* the app, and it's also what syncs
your deadlines in the background every 30 minutes. Closing it stops the app.

`start.bat` self-heals: if the environment ever goes missing or a new version
adds a dependency, it fixes itself and carries on rather than failing.

**Want it one click from your desktop?** Right-click `start.bat` → *Send to*
→ *Desktop (create shortcut)*, then rename the shortcut to whatever you like.

You'll land on a dashboard with an **Add a class** panel. Full instructions
for everything — pulling calendar feeds out of Brightspace, grade tracking,
the study modes, backups, troubleshooting — live inside the app under
**Settings → How to use this**.

### If Windows SmartScreen warns you

`setup.bat` downloads Python from python.org when your machine doesn't have
it. If SmartScreen prompts, that's the Microsoft-signed python.org installer.
Nothing else in this project downloads or installs anything.

### Timezone

Windows reports its timezone in a format Python can't resolve, so set yours
explicitly in `.env`:

```
SCHOOL_AGENT_TIMEZONE=America/New_York
```

This decides what counts as "due today" versus "overdue", so it matters more
than it looks. Without it the app falls back to your machine's current UTC
offset, which is wrong by a full day for an 11:59pm deadline on the other
side of a daylight-saving change.

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
start.bat                    double-click this
setup.bat                    first-run installer, called automatically
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

391 of them, covering the library and every dashboard API route. No network
access, so they run anywhere. A good few are named after bugs that actually
shipped — the comments say what each looked like from a student's side,
because that's the part worth not repeating.

## License

MIT — see LICENSE.
