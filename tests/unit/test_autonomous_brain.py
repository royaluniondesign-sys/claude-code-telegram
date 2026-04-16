"""Tests for AutonomousBrain.generate_strategic_tasks()."""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.brains.autonomous_brain import AutonomousBrain


@pytest.fixture
def brain():
    return AutonomousBrain()


@pytest.fixture
def sample_tasks():
    return [
        {
            "id": "mission-1-1",
            "title": "Answer any Telegram message",
            "tier": "Tier 1",
            "priority": 399,
            "description": "Tier 1 — Foundation: Answer any Telegram message",
            "completed": False,
            "category": "feature",
            "created_by": "mission_parser",
        },
        {
            "id": "mission-2-2",
            "title": "Route tasks to the right brain",
            "tier": "Tier 2",
            "priority": 298,
            "description": "Tier 2 — Intelligence: Route tasks to the right brain",
            "completed": False,
            "category": "feature",
            "created_by": "mission_parser",
        },
    ]


class TestGenerateStrategicTasks:
    def test_returns_list_of_dicts(self, brain, sample_tasks):
        """Tasks are returned as a list of task dicts."""
        with patch(
            "src.utils.mission_parser.parse_mission_file",
            return_value=sample_tasks,
        ):
            result = brain.generate_strategic_tasks()

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(t, dict) for t in result)

    def test_task_has_required_keys(self, brain, sample_tasks):
        """Each task has id, title, tier, priority, description."""
        with patch(
            "src.utils.mission_parser.parse_mission_file",
            return_value=sample_tasks,
        ):
            result = brain.generate_strategic_tasks()

        required = {"id", "title", "tier", "priority", "description"}
        for task in result:
            assert required.issubset(task.keys()), f"Missing keys in {task}"

    def test_returns_empty_list_on_file_not_found(self, brain):
        """Returns [] gracefully when MISSION.md doesn't exist."""
        with patch(
            "src.brains.autonomous_brain.parse_mission_file",
            side_effect=FileNotFoundError("MISSION.md not found"),
        ):
            result = brain.generate_strategic_tasks()

        assert result == []

    def test_returns_empty_list_on_parse_error(self, brain):
        """Returns [] gracefully when parser raises any exception."""
        with patch(
            "src.brains.autonomous_brain.parse_mission_file",
            side_effect=ValueError("invalid format"),
        ):
            result = brain.generate_strategic_tasks()

        assert result == []

    def test_returns_empty_list_when_all_completed(self, brain):
        """Returns [] when mission parser returns no tasks (all completed)."""
        with patch(
            "src.brains.autonomous_brain.parse_mission_file",
            return_value=[],
        ):
            result = brain.generate_strategic_tasks()

        assert result == []

    def test_reads_from_mission_md_path(self, brain):
        """Calls parse_mission_file with the MISSION.md path."""
        expected_path = Path.home() / "claude-code-telegram" / "MISSION.md"
        with patch(
            "src.brains.autonomous_brain.parse_mission_file",
            return_value=[],
        ) as mock_parse:
            brain.generate_strategic_tasks()

        mock_parse.assert_called_once_with(expected_path)
