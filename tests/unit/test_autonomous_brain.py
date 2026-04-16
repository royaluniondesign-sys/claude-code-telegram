from unittest.mock import patch
from src.brains.autonomous_brain import generate_strategic_tasks

def test_strategic_task_generation():
    expected_tasks = [
        {"name": "Task 1", "priority": 1},
        {"name": "Task 2", "priority": 2},
        {"name": "Task 3", "priority": 3}
    ]
    with patch('src.brains.autonomous_brain.generate_strategic_tasks', return_value=expected_tasks):
        from src.brains.autonomous_brain import generate_strategic_tasks
        result = generate_strategic_tasks()
        assert result == expected_tasks, "The generated tasks do not match the expected tasks."
