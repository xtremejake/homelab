"""Tests for the Google Calendar conversion layer (no API keys needed)."""
from datetime import datetime


def test_norm_key_ignores_case_and_whitespace(imp):
    assert imp.norm_key("2026-10-12", "No School") == \
        imp.norm_key("2026-10-12", "  no school ")


def test_api_item_key_all_day(imp):
    item = {"summary": "No School", "start": {"date": "2026-10-12"}}
    assert imp.api_item_key(item) == ("2026-10-12", "no school")


def test_api_item_key_timed(imp):
    item = {"summary": "FrightFest",
            "start": {"dateTime": "2026-10-30T17:00:00-04:00"}}
    assert imp.api_item_key(item) == ("2026-10-30", "frightfest")


def test_all_day_single_day_end_is_next_day(imp):
    body = imp.to_gcal_body(
        {"title": "Harvest Day", "date": "2026-10-08",
         "end_date": "2026-10-08", "notes": ""},
        [], ["foobar@gmail.com"], "America/New_York", "test")
    assert body["start"] == {"date": "2026-10-08"}
    assert body["end"] == {"date": "2026-10-09"}


def test_all_day_multi_day_end_is_exclusive(imp):
    body = imp.to_gcal_body(
        {"title": "Food Drive Week", "date": "2026-11-16",
         "end_date": "2026-11-20", "notes": ""},
        [], [], "America/New_York", "test")
    assert body["start"] == {"date": "2026-11-16"}
    assert body["end"] == {"date": "2026-11-21"}


def test_timed_event_uses_dst_offset_october(imp):
    body = imp.to_gcal_body(
        {"title": "FrightFest", "date": "2026-10-30",
         "start_time": "17:00", "end_time": "19:30", "notes": ""},
        [], [], "America/New_York", "test")
    assert body["start"]["dateTime"] == "2026-10-30T17:00:00-04:00"
    assert body["end"]["dateTime"] == "2026-10-30T19:30:00-04:00"


def test_timed_event_uses_standard_offset_january(imp):
    body = imp.to_gcal_body(
        {"title": "Meeting", "date": "2027-01-13",
         "start_time": "09:30", "end_time": "10:30", "notes": ""},
        [], [], "America/New_York", "test")
    assert body["start"]["dateTime"] == "2027-01-13T09:30:00-05:00"


def test_attendees_and_reminder_overrides(imp):
    reminders = [{"method": "email", "minutes": 10080},
                 {"method": "popup", "minutes": 1440}]
    body = imp.to_gcal_body(
        {"title": "No School", "date": "2026-11-03",
         "end_date": "2026-11-03", "notes": ""},
        reminders, ["foobar@gmail.com", "baz@gmail.com"],
        "America/New_York", "test")
    assert body["attendees"] == [{"email": "foobar@gmail.com"},
                                 {"email": "baz@gmail.com"}]
    assert body["reminders"] == {"useDefault": False,
                                 "overrides": reminders}


def test_no_reminders_means_empty_overrides(imp):
    body = imp.to_gcal_body(
        {"title": "Soup Day", "date": "2026-10-09",
         "end_date": "2026-10-09", "notes": ""},
        [], ["foobar@gmail.com"], "America/New_York", "test")
    assert body["reminders"] == {"useDefault": False, "overrides": []}


def test_build_plan_drops_excluded_and_defaults_missing(imp):
    events = [{"title": "A", "date": "2026-10-01"},
              {"title": "B", "date": "2026-10-02"},
              {"title": "C", "date": "2026-10-03"}]
    decisions = {0: {"include": True, "reminders": []},
                 1: {"include": False, "reminders": []}}
    plan = imp.build_plan(events, decisions)
    assert [e["title"] for e, _ in plan] == ["A", "C"]


def test_reminder_label(imp):
    assert imp.reminder_label([]) == "no reminder"
    assert imp.reminder_label([{"method": "popup", "minutes": 1440}]) == \
        "popup 1d before"
    assert imp.reminder_label([{"method": "email", "minutes": 10080},
                               {"method": "popup", "minutes": 1440}]) == \
        "email 7d before, popup 1d before"


def test_golden_fixture_converts_cleanly(imp, fixture_events):
    """Every event from the real October newsletter converts to a valid
    Calendar API body with a parseable start."""
    assert len(fixture_events) == 29
    for event in fixture_events:
        body = imp.to_gcal_body(event, [], ["foobar@gmail.com"],
                                "America/New_York", "test")
        start = body["start"]
        if "dateTime" in start:
            datetime.fromisoformat(start["dateTime"])
        else:
            datetime.strptime(start["date"], "%Y-%m-%d")
        assert body["summary"] == event["title"]
