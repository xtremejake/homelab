"""Shared fixtures for the school-calendar-import test suite."""
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def load_importer():
    """Load import.py as a module (its filename is a Python keyword)."""
    spec = importlib.util.spec_from_file_location(
        "cal_import", REPO_ROOT / "import.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def imp():
    return load_importer()


@pytest.fixture(scope="session")
def fixture_events():
    import json
    with open(REPO_ROOT / "tests" / "fixtures"
              / "events-october-2026.json") as f:
        return json.load(f)["events"]
