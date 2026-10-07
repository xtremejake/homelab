# School Calendar Import

Turns a school newsletter (Canva PDF/PNG export) into Google Calendar events,
repeatably, on GitHub Actions' free tier.

## TODO

- [ ] Add the real newsletter PDF as `tests/fixtures/newsletter-october-2026.pdf`
  (Canva: Share → Download → PDF). The live end-to-end test
  (`tests/test_end_to_end.py`) self-skips until this file exists — everything
  else in the suite runs without it.

## How it works

```
newsletter.pdf ──▶ Gemini (vision) ──▶ structured events
                                              │
                                              ▼
                                    Gemini (reasoning) ──▶ per-event:
                                    include? reminders?
                                              │
                                              ▼
                        Google Calendar API ──▶ events + attendee invites
```

1. **Extract** — the PDF goes straight to Gemini (it reads PDFs natively, no
   screenshots needed) with an extraction prompt. Output is strict JSON.
2. **Reason** — the events plus a reminder-policy prompt go back to Gemini,
   which decides include/exclude and per-event reminders (popups/emails).
   Both prompts live in `config.example.yaml` and are fully editable.
3. **Write** — events are inserted with attendees, reminders, and
   `sendUpdates=all` so invitees get notified. Re-runs skip events that
   already exist (matched on date + title), so the monthly schedule is safe.

Everything runs in `--dry-run` by default and prints a numbered plan.
`--apply` writes.

## One-time setup (one script, no manual secret juggling)

On your own machine:

```bash
pip install -r requirements.txt
python setup.py
```

It will:
1. Ask for your Gemini API key (hidden input;
   get one at https://aistudio.google.com/apikey).
2. Open your browser once for Google Calendar OAuth approval
   (needs a Google Cloud "Desktop app" OAuth client id/secret —
   you'll be prompted if they're not in `GOOGLE_CLIENT_ID` /
   `GOOGLE_CLIENT_SECRET`).
3. Store all four credentials as **GitHub Secrets** on your repo via the
   `gh` CLI (values go over stdin — never echoed, never committed,
   never written to disk).
4. Ask for the calendar ID and attendee emails and store them as repo
   **Variables** (non-secret, editable anytime under repo Settings).

Example of what it asks:

```
Google Calendar ID (e.g. foobar-family@group.calendar.google.com): foobar-family@group.calendar.google.com
Attendee emails, comma-separated (e.g. foobar@gmail.com): foobar@gmail.com,baz@gmail.com
```

Re-running `setup.py` is safe — it just overwrites the stored values
(handy when a key rotates). Add `--trigger-dry-run --file newsletter.pdf`
to kick off a dry-run right after setup.

Manual alternative: repo → Settings → Secrets and variables → Actions, and
add `GEMINI_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`GOOGLE_REFRESH_TOKEN` as Secrets and `CALENDAR_ID`, `ATTENDEE_EMAILS`
as Variables yourself.

## Usage

**Manual run** (recommended while tuning prompts):

```bash
# local dry-run first -- prints the numbered plan, writes nothing
python import.py --input incoming/newsletter.pdf --dry-run \
  --calendar-id foobar-family@group.calendar.google.com \
  --attendees foobar@gmail.com,baz@gmail.com

# happy with the plan? write it
python import.py --input incoming/newsletter.pdf --apply \
  --calendar-id foobar-family@group.calendar.google.com \
  --attendees foobar@gmail.com
```

Or via Actions: put the exported PDF in `incoming/` (commit it, or upload
via the GitHub web UI), then Actions → "School calendar import" →
**Run workflow** → file name, calendar ID (e.g.
`foobar-family@group.calendar.google.com`), attendees (e.g.
`foobar@gmail.com`) → start with `dry-run`, read the plan in the run log,
re-run with `apply`.

**Scheduled run**: on the 1st of each month it processes any new files in
`incoming/`, applies them, and moves them to `processed/` automatically.

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

- `tests/test_gcal.py` — Calendar API body conversion (all-day, multi-day,
  DST-aware timed events, attendees, reminders, dedup keys) plus a golden
  test converting all 29 events from the real October fixture.
- `tests/test_policy.py` — asserts the reminder policy we agreed on stays
  encoded in `config.example.yaml` (no-school → 7-day email + 1-day popup,
  spirit days → 1-day popup, informational → none, K/1st-grade filter).
- `tests/test_end_to_end.py` — live extraction test against
  `tests/fixtures/newsletter-october-2026.pdf`; self-skips without
  `GEMINI_API_KEY`.
- `.github/workflows/tests.yml` runs the suite on every push/PR touching
  `school-calendar-import/`.

## Files

| File | What it is |
|---|---|
| `import.py` | the importer (extract → reason → write) |
| `setup.py` | one-command setup: OAuth → GitHub Secrets/Variables → optional dry-run |
| `config.example.yaml` | prompts + defaults (no secrets, no personal data) |
| `requirements.txt` / `requirements-dev.txt` | runtime / test deps |
| `tests/` | pytest suite + real October newsletter fixtures |
| `../.github/workflows/school-calendar-import.yml` | import workflow (place at repo root) |
| `../.github/workflows/tests.yml` | test workflow (place at repo root) |

## Customizing

- **Different school / grades**: edit `reminder_policy_prompt` in the config.
  `tests/test_policy.py` guards the current rules — update it to match.
- **Different LLM**: the Gemini calls are isolated in `extract_events()` /
  `decide_reminders()` — swap the client, keep the JSON schemas.
- **Costs**: Gemini free tier + Actions free tier (2,000 min/mo) comfortably
  cover a monthly newsletter run.
