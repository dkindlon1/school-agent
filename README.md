# School Agent

A study assistant that only cares about one thing: that you get good grades.

It syncs every class's deadlines from your school's calendar, tracks what each
assignment is actually worth, and - when you sit down to work - puts a tutor in
front of you that knows your course material. It runs entirely on your own
machine. Nothing leaves it except the text of whatever you ask a model about,
and only to the provider you configured.

## Pick your folder

| You're on | Open | Then double-click |
| --- | --- | --- |
| Windows | [`school-agent-windows/`](school-agent-windows/) | `setup.bat` |
| macOS | [`school-agent-mac/`](school-agent-mac/) | `setup.sh` |

**Take one folder, not the repo.** Each is a complete, standalone project with
its own installer, its own tests and its own README - they share no code and
neither imports the other. They live in one repo for convenience, nothing more.
Copy the folder you want anywhere on your machine and it works on its own.

## What it does

- **Deadlines, synced.** Reads your school's calendar feed (Brightspace/D2L and
  anything else that exports ICS) every 30 minutes while it's running.
- **Grades that mean something.** Point it at your syllabus and it reads the
  grading table, then answers the only question that matters: what do I need on
  what's left, and which of these three things due this week is worth doing first.
- **Seven ways to study.** Flashcards on an FSRS spaced-repetition schedule,
  worked examples, guided practice with hints you unlock one at a time,
  explain-it-back, why-questions, concept maps, interleaved drills.
- **Struggle ladders.** Say what keeps costing you marks and it builds practice
  problems whose support fades one rung at a time - fully worked, then you
  finish it, then you do the hard part, then just the principle, then on your
  own. Getting one wrong drops you back a rung.
- **A daily briefing** ordered by grade impact rather than by date.

## Your data stays yours

Your coursework, your keys and your classes live in `data/`, `.env` and
`config/classes.yaml` inside whichever folder you're using. All three are
gitignored in both projects and none of them is in this repository.

## License

MIT - see the LICENSE file in either folder.
