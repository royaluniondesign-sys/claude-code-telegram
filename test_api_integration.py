#!/usr/bin/env python3
"""Test API integration: verify endpoints return correct data for dashboard."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))


def test_conductor_history_format() -> bool:
    """Verify conductor_history returns correct format."""
    from src.infra.conductor_history import get_history, history_stats

    runs = get_history(limit=5)
    stats = history_stats()

    print("✓ conductor_history.get_history():", len(runs), "runs")
    print("✓ conductor_history.history_stats():", stats)

    # Check stats format
    required_stats = {"total", "success", "failed", "avg_duration_ms"}
    if not all(k in stats for k in required_stats):
        print(f"✗ Missing stats fields. Got: {stats.keys()}")
        return False

    # Check run format (if any)
    if runs:
        run = runs[0]
        required_run_fields = {
            "run_id", "task", "source", "started_at",
            "steps_completed", "steps_failed", "is_error", "total_duration_ms"
        }
        missing = required_run_fields - set(run.keys())
        if missing:
            print(f"✗ Missing run fields: {missing}")
            print(f"  Got: {run.keys()}")
            return False
        print(f"✓ Run format valid: {run['run_id'][:8]}... source={run['source']}")
    else:
        print("  (no runs in history, format check skipped)")

    return True


def test_proactive_status_format() -> bool:
    """Verify proactive_loop returns correct format."""
    from src.infra.proactive_loop import get_proactive_status

    status = get_proactive_status()
    print("✓ proactive_loop.get_proactive_status():", status)

    # Check format
    required_fields = {
        "running", "last_run_at", "next_run_at",
        "total_steps_ok", "total_steps_failed", "total_runs"
    }
    missing = required_fields - set(status.keys())
    if missing:
        print(f"✗ Missing status fields: {missing}")
        print(f"  Got: {status.keys()}")
        return False

    # Validate types
    if not isinstance(status["running"], bool):
        print(f"✗ 'running' should be bool, got {type(status['running'])}")
        return False

    return True


async def test_api_endpoints() -> bool:
    """Test actual API endpoints via FastAPI test client."""
    from fastapi.testclient import TestClient
    from src.api.server import app

    client = TestClient(app)

    print("\n--- Testing API Endpoints ---")

    # Test /api/conductor/history
    print("\nGET /api/conductor/history")
    resp = client.get("/api/conductor/history")
    if resp.status_code != 200:
        print(f"✗ Status {resp.status_code}")
        return False

    hist = resp.json()
    print(f"✓ Status 200, response: {json.dumps(hist, indent=2)[:200]}...")

    if not hist.get("ok"):
        print("✗ 'ok' should be True")
        return False

    if "runs" not in hist or "stats" not in hist:
        print(f"✗ Missing 'runs' or 'stats' in response")
        return False

    # Test /api/proactive/status
    print("\nGET /api/proactive/status")
    resp = client.get("/api/proactive/status")
    if resp.status_code != 200:
        print(f"✗ Status {resp.status_code}")
        return False

    pstat = resp.json()
    print(f"✓ Status 200, response: {json.dumps(pstat, indent=2)[:200]}...")

    if not pstat.get("ok"):
        print("✗ 'ok' should be True")
        return False

    # Check proactive fields
    required = {"running", "total_steps_ok", "total_steps_failed", "total_runs"}
    missing = required - set(pstat.keys())
    if missing:
        print(f"✗ Missing fields in proactive status: {missing}")
        return False

    return True


def main():
    """Run all tests."""
    print("=== API Integration Tests ===\n")

    tests = [
        ("conductor_history format", test_conductor_history_format),
        ("proactive_status format", test_proactive_status_format),
        ("API endpoints (FastAPI)", test_api_endpoints),
    ]

    results = []
    for name, test_fn in tests:
        print(f"\n--- {name} ---")
        try:
            if asyncio.iscoroutinefunction(test_fn):
                result = asyncio.run(test_fn())
            else:
                result = test_fn()
            results.append((name, result))
            print(f"{'✓ PASS' if result else '✗ FAIL'}")
        except Exception as e:
            print(f"✗ EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    print("\n=== Summary ===")
    for name, result in results:
        print(f"{'✓' if result else '✗'} {name}")

    passed = sum(1 for _, r in results if r)
    total = len(results)
    print(f"\n{passed}/{total} tests passed")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
