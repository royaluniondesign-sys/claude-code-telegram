"""Parse MISSION.md and generate strategic tasks for autonomous development.

This module reads the MISSION.md file and extracts uncompleted tasks,
organizing them by priority tier and creating executable task objects
that the autonomous brain can work on.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def parse_mission_file(file_path: Path | str) -> List[Dict[str, Any]]:
    """Parse MISSION.md and extract strategic tasks.

    Args:
        file_path: Path to MISSION.md file

    Returns:
        List of task dictionaries with structure:
        {
            "id": "task-id",
            "title": "short description",
            "tier": "Tier N",
            "priority": 1-10 (higher = more urgent),
            "description": "full description",
            "completed": False,
        }

    Raises:
        FileNotFoundError: If mission file does not exist
        ValueError: If mission file format is invalid
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"Mission file not found: {file_path}")

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        raise ValueError(f"Failed to read mission file: {e}") from e

    tasks: List[Dict[str, Any]] = []
    task_counter = 1

    # Parse tier sections (e.g., "### Tier 1 — Foundation")
    tier_pattern = r"### (Tier \d+.*?)\n((?:- \[.\].*?\n)*)"
    tier_matches = re.finditer(tier_pattern, content)

    for tier_match in tier_matches:
        tier_title = tier_match.group(1).strip()
        tier_num = int(re.search(r"Tier (\d+)", tier_title).group(1))
        tier_items = tier_match.group(2)

        # Parse individual items (e.g., "- [ ] Item description")
        item_pattern = r"- \[([ xX])\] (.*?)(?=\n|$)"
        item_matches = re.finditer(item_pattern, tier_items)

        for item_match in item_matches:
            checkbox = item_match.group(1)
            description = item_match.group(2).strip()

            # Skip completed items (marked with [x] or [X])
            is_completed = checkbox.lower() == "x"
            if is_completed:
                continue

            # Priority: Tier 1 (highest), Tier 4 (lowest)
            # Within tier: lower item number = higher priority
            priority = (5 - tier_num) * 100 - task_counter

            task: Dict[str, Any] = {
                "id": f"mission-{tier_num}-{task_counter}",
                "title": description,
                "tier": f"Tier {tier_num}",
                "priority": priority,
                "description": f"{tier_title}: {description}",
                "completed": False,
                "category": "feature",
                "created_by": "mission_parser",
            }
            tasks.append(task)
            task_counter += 1

    # Sort by priority (higher = more urgent)
    tasks.sort(key=lambda t: t["priority"], reverse=True)

    return tasks


def get_next_strategic_task(file_path: Path | str) -> Optional[Dict[str, Any]]:
    """Get the highest-priority uncompleted task from MISSION.md.

    Args:
        file_path: Path to MISSION.md file

    Returns:
        The next task to work on, or None if all tasks are completed
    """
    tasks = parse_mission_file(file_path)
    return tasks[0] if tasks else None
