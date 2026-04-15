"""API test execution and validation.

Runs integration tests against AURA's API endpoints and tracks failures.
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()


@dataclass
class TestResult:
    """Result of a single test run."""
    test_name: str
    passed: bool
    duration: float
    error_msg: Optional[str] = None


@dataclass
class TestSummary:
    """Summary of test suite execution."""
    total: int
    passed: int
    failed: int
    errors: int
    skipped: int
    duration: float
    success: bool


def run_tests() -> TestSummary:
    """Run API integration tests.

    Returns:
        TestSummary with aggregated test results.
    """
    try:
        result = subprocess.run(
            [
                "python3",
                "-m",
                "pytest",
                "tests/integration/",
                "-v",
                "--tb=short",
                "--durations=10",
                "-x",  # stop on first failure
            ],
            cwd="/Users/oxyzen/claude-code-telegram",
            capture_output=True,
            text=True,
            timeout=180,
        )

        return _parse_test_output(result.stdout, result.stderr, result.returncode)
    except subprocess.TimeoutExpired:
        logger.error("test_timeout", timeout_sec=180)
        return TestSummary(
            total=0,
            passed=0,
            failed=0,
            errors=1,
            skipped=0,
            duration=180.0,
            success=False,
        )
    except Exception as e:
        logger.error("test_execution_failed", error=str(e))
        return TestSummary(
            total=0,
            passed=0,
            failed=0,
            errors=1,
            skipped=0,
            duration=0.0,
            success=False,
        )


def run_unit_tests() -> TestSummary:
    """Run unit tests only (faster, no external dependencies).

    Returns:
        TestSummary with unit test results.
    """
    try:
        result = subprocess.run(
            [
                "python3",
                "-m",
                "pytest",
                "tests/unit/",
                "-v",
                "--tb=line",
            ],
            cwd="/Users/oxyzen/claude-code-telegram",
            capture_output=True,
            text=True,
            timeout=60,
        )

        return _parse_test_output(result.stdout, result.stderr, result.returncode)
    except subprocess.TimeoutExpired:
        logger.error("unit_test_timeout", timeout_sec=60)
        return TestSummary(
            total=0,
            passed=0,
            failed=0,
            errors=1,
            skipped=0,
            duration=60.0,
            success=False,
        )
    except Exception as e:
        logger.error("unit_test_failed", error=str(e))
        return TestSummary(
            total=0,
            passed=0,
            failed=0,
            errors=1,
            skipped=0,
            duration=0.0,
            success=False,
        )


def test_api_endpoints() -> dict:
    """Quick sanity check on critical API endpoints.

    Returns:
        Dict with endpoint health status.
    """
    try:
        result = subprocess.run(
            [
                "python3",
                "-m",
                "pytest",
                "tests/integration/test_api.py",
                "-v",
                "-k",
                "health or status",
            ],
            cwd="/Users/oxyzen/claude-code-telegram",
            capture_output=True,
            text=True,
            timeout=30,
        )

        return {
            "endpoints_ok": result.returncode == 0,
            "output": result.stdout[:500],
        }
    except Exception as e:
        return {
            "endpoints_ok": False,
            "output": str(e),
        }


def _parse_test_output(stdout: str, stderr: str, returncode: int) -> TestSummary:
    """Parse pytest output to extract test statistics.

    Args:
        stdout: Standard output from pytest
        stderr: Standard error from pytest
        returncode: Process return code

    Returns:
        Parsed TestSummary.
    """
    output = stdout + stderr
    lines = output.split("\n")

    # Look for the summary line: "X passed, Y failed, Z skipped in 1.23s"
    summary_line = next(
        (l.strip() for l in reversed(lines) if " passed" in l or " failed" in l),
        "",
    )

    # Extract counts (simple regex-free parsing)
    passed = _extract_number(summary_line, "passed")
    failed = _extract_number(summary_line, "failed")
    skipped = _extract_number(summary_line, "skipped")
    errors = _extract_number(summary_line, "error")
    duration = _extract_duration(summary_line)
    total = passed + failed + skipped + errors

    return TestSummary(
        total=total,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        duration=duration,
        success=returncode == 0,
    )


def _extract_number(text: str, keyword: str) -> int:
    """Extract a count from pytest summary line."""
    try:
        parts = text.split()
        for i, part in enumerate(parts):
            if keyword in part and i > 0:
                return int(parts[i - 1])
    except (ValueError, IndexError):
        pass
    return 0


def _extract_duration(text: str) -> float:
    """Extract duration from pytest summary line."""
    try:
        import re
        match = re.search(r"([\d.]+)s", text)
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return 0.0
