"""Spec tests: the reminder policy we agreed on must stay encoded in the
config's prompt. If someone edits the prompt and drops a rule, these fail."""
from pathlib import Path

import pytest
import yaml

CONFIG = Path(__file__).parent.parent / "config.example.yaml"


@pytest.fixture(scope="module")
def policy():
    with open(CONFIG) as f:
        cfg = yaml.safe_load(f)
    return cfg["reminder_policy_prompt"].lower()


def test_no_school_days_get_advance_planning(policy):
    assert "no-school" in policy or "no school" in policy
    assert "10080" in policy  # 7-day email
    assert "childcare" in policy or "advance planning" in policy


def test_half_days_get_advance_planning(policy):
    assert "half" in policy


def test_spirit_days_get_day_before_popup(policy):
    assert "spirit" in policy
    assert "1440" in policy  # 1-day popup


def test_informational_days_get_no_reminders(policy):
    assert "no reminders" in policy
    for keyword in ("appreciation", "book fair"):
        assert keyword in policy


def test_grade_filter_targets_kindergarten_and_first_grade(policy):
    assert "kindergarten" in policy
    assert "first grade" in policy
    for excluded in ("5th", "2nd", "pre-k"):
        assert excluded in policy


def test_config_has_no_real_personal_data():
    text = CONFIG.read_text()
    for leaked in ("renatevginderen", "jdmarold", "xtremejake"):
        assert leaked not in text
