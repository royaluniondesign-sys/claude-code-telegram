"""Tests for self-repair functionality."""

import pytest


def test_self_repair():
    """Test self-repair workflow."""
    # Arrange
    from aura.self_repair import SelfRepair

    # Act & Assert
    self_repair = SelfRepair()
    self_repair.diagnose()
    self_repair.repair()

    assert self_repair.is_repaired(), "Self-repair did not complete successfully"
