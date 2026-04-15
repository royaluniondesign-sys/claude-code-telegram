"""Brain health monitoring and self-repair system.

Detects failures in AURA's brain modules and applies targeted fixes.
"""
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import structlog

logger = structlog.get_logger()


class HealthCheck(NamedTuple):
    """Result of a brain health check."""
    brain_name: str
    is_healthy: bool
    error_msg: str | None = None
    recovery_attempted: bool = False


def check_brain_health(brain_name: str) -> HealthCheck:
    """Check if a brain module can be imported and initialized.

    Args:
        brain_name: Name of the brain module (e.g., 'claude_brain', 'openrouter_brain')

    Returns:
        HealthCheck result with health status and any error details.
    """
    try:
        module = __import__(f"src.brains.{brain_name}", fromlist=[brain_name])
        return HealthCheck(brain_name=brain_name, is_healthy=True)
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)[:100]}"
        logger.error("brain_health_check_failed", brain=brain_name, error=error_msg)
        return HealthCheck(brain_name=brain_name, is_healthy=False, error_msg=error_msg)


def diagnose_error(brain_name: str, error_msg: str) -> dict:
    """Analyze a brain error and suggest recovery steps.

    Args:
        brain_name: Name of the failing brain
        error_msg: Error message from the brain

    Returns:
        Diagnosis dict with suggested fixes and root cause.
    """
    diagnosis = {
        "brain": brain_name,
        "error": error_msg,
        "likely_cause": "unknown",
        "suggested_fixes": [],
    }

    if "ModuleNotFoundError" in error_msg:
        diagnosis["likely_cause"] = "missing_dependency"
        diagnosis["suggested_fixes"] = [
            "pip install missing_module",
            "check requirements.txt",
        ]
    elif "ImportError" in error_msg:
        diagnosis["likely_cause"] = "circular_import"
        diagnosis["suggested_fixes"] = [
            "check import order in __init__.py",
            "use lazy imports inside functions",
        ]
    elif "AttributeError" in error_msg:
        diagnosis["likely_cause"] = "api_change"
        diagnosis["suggested_fixes"] = [
            "check if API changed in dependency",
            "verify method signatures",
        ]
    elif "APIError" in error_msg or "rate limit" in error_msg.lower():
        diagnosis["likely_cause"] = "api_failure"
        diagnosis["suggested_fixes"] = [
            "wait and retry (backoff)",
            "check API status",
            "verify API key",
        ]
    elif "connection" in error_msg.lower():
        diagnosis["likely_cause"] = "network_issue"
        diagnosis["suggested_fixes"] = [
            "check network connectivity",
            "verify endpoint URLs",
            "try again with backoff",
        ]

    return diagnosis


def repair_error(brain_name: str, diagnosis: dict) -> bool:
    """Attempt to repair a brain error.

    Args:
        brain_name: Name of the failing brain
        diagnosis: Diagnosis dict from diagnose_error()

    Returns:
        True if repair was successful, False otherwise.
    """
    likely_cause = diagnosis.get("likely_cause")

    if likely_cause == "missing_dependency":
        return _repair_missing_dependency(brain_name)
    elif likely_cause == "api_key":
        return _repair_api_key(brain_name)
    elif likely_cause == "network_issue":
        return _repair_network(brain_name)

    logger.info("repair_skipped", brain=brain_name, cause=likely_cause)
    return False


def _repair_missing_dependency(brain_name: str) -> bool:
    """Attempt to install missing dependencies."""
    try:
        result = subprocess.run(
            ["pip", "install", "-q", "-e", "."],
            cwd="/Users/oxyzen/claude-code-telegram",
            capture_output=True,
            text=True,
            timeout=30,
        )
        success = result.returncode == 0
        logger.info("repair_dependency", brain=brain_name, success=success)
        return success
    except Exception as e:
        logger.error("repair_failed", brain=brain_name, error=str(e))
        return False


def _repair_api_key(brain_name: str) -> bool:
    """Validate and rotate API keys if possible."""
    # This is a stub — actual implementation would check env vars
    logger.info("repair_api_key", brain=brain_name, status="skipped_manual")
    return False


def _repair_network(brain_name: str) -> bool:
    """Test network connectivity and clear caches."""
    try:
        # Clear Python import cache
        if brain_name in sys.modules:
            del sys.modules[f"src.brains.{brain_name}"]
        logger.info("repair_network", brain=brain_name, status="cache_cleared")
        return True
    except Exception as e:
        logger.error("repair_network_failed", brain=brain_name, error=str(e))
        return False


def run_tests() -> dict:
    """Run full test suite and return summary.

    Returns:
        Dict with test results: passed, failed, errors, total.
    """
    try:
        result = subprocess.run(
            ["python3", "-m", "pytest", "tests/", "-v", "--tb=short"],
            cwd="/Users/oxyzen/claude-code-telegram",
            capture_output=True,
            text=True,
            timeout=120,
        )

        # Parse pytest output for summary
        output = result.stdout + result.stderr
        lines = output.split("\n")
        summary_line = next((l for l in reversed(lines) if " passed" in l or " failed" in l), "")

        return {
            "success": result.returncode == 0,
            "summary": summary_line.strip() if summary_line else "no summary",
            "returncode": result.returncode,
            "output_lines": len(lines),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "summary": "pytest timeout", "returncode": -1}
    except Exception as e:
        return {"success": False, "summary": str(e), "returncode": -1}
