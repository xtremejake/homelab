"""Live end-to-end test against the real fixture PDF.

Requires GEMINI_API_KEY (and network). Skipped otherwise -- run it locally
or as a manual workflow when you want to verify extraction quality:

    GEMINI_API_KEY=... pytest tests/test_end_to_end.py -v
"""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY not set -- live LLM test skipped")

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "newsletter-october-2026.pdf"


@pytest.fixture(scope="module")
def extracted(imp):
    pytest.importorskip("google.genai")
    if not FIXTURE_PDF.exists():
        pytest.skip("fixture PDF not present")
    client = imp.gemini()
    import yaml
    cfg = yaml.safe_load(
        (Path(__file__).parent.parent / "config.example.yaml").read_text())
    return imp.extract_events(client, cfg["llm"]["model"],
                              str(FIXTURE_PDF), cfg["extraction_prompt"])


def test_extraction_finds_events(extracted):
    assert len(extracted) >= 20, \
        f"expected a full newsletter of events, got {len(extracted)}"


def test_extraction_has_expected_events(extracted):
    titles = [e["title"].lower() for e in extracted]
    blob = "\n".join(titles)
    for expected in ("no school", "frightfest", "thanksgiving",
                     "kindergarten identity day"):
        assert expected in blob, f"missing expected event: {expected}"


def test_extraction_dates_are_sane(extracted):
    from datetime import datetime
    for e in extracted:
        d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        assert datetime(2026, 9, 1).date() <= d <= \
            datetime(2027, 8, 31).date(), f"suspicious date {e}"
