#!/usr/bin/env python3
"""
Newsletter -> Google Calendar importer.

Pipeline:
  1. EXTRACT: send the newsletter PDF/PNG to Gemini (vision) and get back
     structured events: [{title, date, end_date, start_time, end_time,
     location, grades, notes}]
  2. REASON: send the events + a reminder-policy prompt to Gemini and get
     back per-event decisions: [{index, include, reason, reminders}]
  3. WRITE: insert included events into Google Calendar with attendees,
     reminders, and notifications (dry-run by default; --apply to write).

Secrets come from the environment (GitHub Secrets in Actions) -- never from
the repo:
  GEMINI_API_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN

Non-secret settings come from config.yaml, CLI flags, or env:
  --calendar-id / CALENDAR_ID, --attendees / ATTENDEE_EMAILS, --timezone

Usage:
  python import.py --input incoming/newsletter.pdf --dry-run
  python import.py --input incoming/newsletter.pdf --apply \\
      --calendar-id foobar-family@group.calendar.google.com \\
      --attendees foobar@gmail.com
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

# NOTE: google.* imports are lazy (inside functions) so the pure conversion
# helpers remain importable/testable without the API client libraries.

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
MIME_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg"}

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "date": {"type": "string",
                             "description": "Start date as YYYY-MM-DD"},
                    "end_date": {"type": "string",
                                 "description": "End date as YYYY-MM-DD for multi-day events, else same as date"},
                    "start_time": {"type": "string",
                                   "description": "Start time as HH:MM 24h, or empty string"},
                    "end_time": {"type": "string",
                                 "description": "End time as HH:MM 24h, or empty string"},
                    "location": {"type": "string"},
                    "grades": {"type": "array", "items": {"type": "string"},
                               "description": 'e.g. ["kindergarten"], ["5th"], or ["all"]'},
                    "notes": {"type": "string"},
                },
                "required": ["title", "date"],
            },
        }
    },
    "required": ["events"],
}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer",
                              "description": "Position of the event in the input list, starting at 0"},
                    "include": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "reminders": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "method": {"type": "string",
                                           "description": '"popup" or "email"'},
                                "minutes": {"type": "integer",
                                            "description": "Minutes before the event"},
                            },
                            "required": ["method", "minutes"],
                        },
                    },
                },
                "required": ["index", "include", "reminders"],
            },
        }
    },
    "required": ["decisions"],
}


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def gemini():
    from google import genai
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        die("GEMINI_API_KEY is not set")
    return genai.Client(api_key=key)


def as_json(text):
    """Parse model output defensively (strip code fences if present)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    return json.loads(text)


def extract_events(client, model, input_path, prompt):
    from google.genai import types
    suffix = Path(input_path).suffix.lower()
    mime = MIME_TYPES.get(suffix)
    if not mime:
        die(f"Unsupported file type: {suffix} (use pdf, png, jpg)")
    data = Path(input_path).read_bytes()
    print(f"Extracting events from {input_path} ...")
    resp = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=data, mime_type=mime), prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=EVENT_SCHEMA,
        ),
    )
    events = as_json(resp.text)["events"]
    print(f"  found {len(events)} events")
    return events


def decide_reminders(client, model, events, policy_prompt):
    from google.genai import types
    print("Deciding reminder policy ...")
    prompt = policy_prompt + "\n\nEVENTS (JSON):\n" + json.dumps(events, indent=1)
    resp = client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=DECISION_SCHEMA,
        ),
    )
    decisions = as_json(resp.text)["decisions"]
    by_index = {d["index"]: d for d in decisions}
    return by_index


def to_gcal_body(event, reminders, attendee_emails, timezone, source):
    body = {
        "summary": event["title"],
        "description": ((event.get("notes") or "")
                        + f"\n\nSource: {source}").strip(),
        "attendees": [{"email": e} for e in attendee_emails],
        "reminders": {"useDefault": False,
                      "overrides": reminders},
    }
    if event.get("location"):
        body["location"] = event["location"]
    tz = ZoneInfo(timezone)
    start_time = event.get("start_time") or ""
    if start_time:
        end_time = event.get("end_time") or ""
        s = datetime.strptime(f"{event['date']}T{start_time}",
                              "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
        body["start"] = {"dateTime": s.isoformat()}
        if end_time:
            e = datetime.strptime(
                f"{event.get('end_date') or event['date']}T{end_time}",
                "%Y-%m-%dT%H:%M").replace(tzinfo=tz)
        else:
            e = s + timedelta(hours=1)
        body["end"] = {"dateTime": e.isoformat()}
    else:
        end_date = event.get("end_date") or event["date"]
        end_excl = (datetime.strptime(end_date, "%Y-%m-%d")
                    + timedelta(days=1)).strftime("%Y-%m-%d")
        body["start"] = {"date": event["date"]}
        body["end"] = {"date": end_excl}
    return body


def norm_key(date_str, summary):
    """Dedup key for an event: (date, normalized summary). Pure function,
    also used by tests."""
    return (date_str, summary.strip().lower())


def build_plan(events, decisions):
    """Pair extracted events with their reminder decisions, dropping the
    ones the policy excluded. Pure function, also used by tests."""
    plan = []
    for i, event in enumerate(events):
        d = decisions.get(i, {"include": True, "reminders": [],
                              "reason": "no decision returned"})
        if d.get("include"):
            plan.append((event, d))
    return plan


def existing_keys(service, calendar_id, events):
    """(date, normalized summary) for events already on the calendar, so
    re-runs don't create duplicates."""
    dates = sorted({e["date"] for e in events} |
                   {e.get("end_date") or e["date"] for e in events})
    if not dates:
        return set()
    time_min = f"{dates[0]}T00:00:00Z"
    time_max = (datetime.strptime(dates[-1], "%Y-%m-%d")
                + timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
    keys = set()
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id, timeMin=time_min, timeMax=time_max,
            singleEvents=True, maxResults=250, pageToken=page_token).execute()
        for item in resp.get("items", []):
            keys.add(api_item_key(item))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return keys


def api_item_key(item):
    """Dedup key for an event already on the calendar (Calendar API shape)."""
    start = item.get("start", {})
    d = (start.get("dateTime") or start.get("date", ""))[:10]
    return norm_key(d, item.get("summary", ""))


def calendar_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    for var in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                "GOOGLE_REFRESH_TOKEN"):
        if not os.environ.get(var):
            die(f"{var} is not set")
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[CALENDAR_SCOPE],
    )
    return build("calendar", "v3", credentials=creds)


def reminder_label(reminders):
    if not reminders:
        return "no reminder"
    parts = []
    for r in reminders:
        mins = r["minutes"]
        when = (f"{mins // 1440}d" if mins % 1440 == 0
                else f"{mins // 60}h" if mins % 60 == 0
                else f"{mins}m")
        parts.append(f"{r['method']} {when} before")
    return ", ".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input", required=True, help="Newsletter PDF/PNG path")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--calendar-id", default=os.environ.get("CALENDAR_ID", ""))
    ap.add_argument("--attendees", default=os.environ.get("ATTENDEE_EMAILS", ""),
                    help="Comma-separated attendee emails")
    ap.add_argument("--timezone", default=os.environ.get("TIMEZONE", ""))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    calendar_id = args.calendar_id or cfg.get("calendar_id", "")
    if not calendar_id:
        die("No calendar id: pass --calendar-id, set CALENDAR_ID, or put it in config")
    attendees = [e.strip() for e in
                 (args.attendees or ",".join(cfg.get("attendees", []))).split(",")
                 if e.strip()]
    timezone = args.timezone or cfg.get("timezone", "America/New_York")
    llm = cfg.get("llm", {})
    model = llm.get("model", "gemini-2.5-flash")

    client = gemini()
    events = extract_events(client, model, args.input,
                            cfg["extraction_prompt"])
    decisions = decide_reminders(client, model, events,
                                 cfg["reminder_policy_prompt"])

    plan = build_plan(events, decisions)

    print(f"\nPlan: {len(plan)} of {len(events)} events to add "
          f"(dry-run={not args.apply})\n")
    for n, (event, d) in enumerate(plan, 1):
        when = event["date"]
        if event.get("start_time"):
            when += f" {event['start_time']}"
        print(f"{n}. {when} — {event['title']}")
        print(f"   reminders: {reminder_label(d['reminders'])}"
              + (f"  ({d.get('reason', '')})" if d.get("reason") else ""))

    if not args.apply:
        print("\nDry run complete. Re-run with --apply to write to the calendar.")
        return

    service = calendar_service()
    known = existing_keys(service, calendar_id,
                          [e for e, _ in plan])
    created, skipped = 0, []
    for event, d in plan:
        if norm_key(event["date"], event["title"]) in known:
            skipped.append(event["title"])
            continue
        body = to_gcal_body(event, d["reminders"], attendees, timezone,
                            source=f"newsletter import ({Path(args.input).name})")
        service.events().insert(calendarId=calendar_id, body=body,
                                sendUpdates="all").execute()
        created += 1
        print(f"  + {event['date']} {event['title']}")

    print(f"\nDone: created {created}, skipped {len(skipped)} duplicates.")
    for s in skipped:
        print(f"  = already present: {s}")


if __name__ == "__main__":
    main()
