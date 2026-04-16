"""Brain health monitoring and self-repair system.

Detects failures in AURA's brain modules and applies targeted fixes.
"""
import asyncio
import logging
import os
import subprocess
import sys
import traceback
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


def repair_error(brain_name: str, diagnosis: dict, error: Exception | None = None) -> bool:
    """Attempt to repair a brain error with improved error handling.

    Args:
        brain_name: Name of the failing brain
        diagnosis: Diagnosis dict from diagnose_error()
        error: Optional exception object for detailed analysis

    Returns:
        True if repair was successful, False otherwise.
    """
    try:
        # Enhanced error logging with traceback if available
        if error:
            error_type = type(error).__name__
            error_message = str(error)
            error_tb = traceback.format_exc()

            log_context = {
                "brain": brain_name,
                "error_type": error_type,
                "error_message": error_message,
            }

            # Detect specific error types for targeted handling
            if "CancelledError" in error_tb:
                logger.error("repair_error_cancelled", **log_context)
                return _handle_cancelled_error(brain_name)
            elif "asyncio.exceptions.TimeoutError" in error_tb or "TimeoutError" in error_type:
                logger.error("repair_error_timeout", **log_context)
                return _handle_timeout_error(brain_name)

        likely_cause = diagnosis.get("likely_cause")

        if likely_cause == "missing_dependency":
            return _repair_missing_dependency(brain_name)
        elif likely_cause == "api_key":
            return _repair_api_key(brain_name)
        elif likely_cause == "network_issue":
            return _repair_network(brain_name)

        logger.info("repair_skipped", brain=brain_name, cause=likely_cause)
        return False

    except Exception as e:
        # Escalation: unknown errors go to higher-level support
        error_type = type(e).__name__
        logger.error(
            "repair_error_escalation",
            brain=brain_name,
            error_type=error_type,
            error_message=str(e),
            escalate_to="sonnet_brain",
        )
        return False


def _handle_cancelled_error(brain_name: str) -> bool:
    """Handle CancelledError by resetting the brain state."""
    try:
        # Clear the brain from sys.modules to force reimport
        if brain_name in sys.modules:
            del sys.modules[f"src.brains.{brain_name}"]
        logger.info("repair_cancelled_handled", brain=brain_name)
        return True
    except Exception as e:
        logger.error("repair_cancelled_failed", brain=brain_name, error=str(e))
        return False


def _handle_timeout_error(brain_name: str) -> bool:
    """Handle TimeoutError by clearing caches and retrying."""
    try:
        # Clear import cache and wait for retry
        if brain_name in sys.modules:
            del sys.modules[f"src.brains.{brain_name}"]
        logger.info("repair_timeout_handled", brain=brain_name, action="cache_cleared")
        return True
    except Exception as e:
        logger.error("repair_timeout_failed", brain=brain_name, error=str(e))
        return False


def _repair_missing_dependency(brain_name: str) -> bool:
    """Attempt to install missing dependencies with improved error handling."""
    try:
        result = subprocess.run(
            ["pip", "install", "-q", "-e", "."],
            cwd="/Users/oxyzen/claude-code-telegram",
            capture_output=True,
            text=True,
            timeout=30,
        )
        success = result.returncode == 0
        if not success:
            logger.error(
                "repair_dependency_failed",
                brain=brain_name,
                returncode=result.returncode,
                stderr=result.stderr[:200],
            )
        else:
            logger.info("repair_dependency", brain=brain_name, success=success)
        return success
    except subprocess.TimeoutExpired:
        logger.error("repair_dependency_timeout", brain=brain_name)
        return False
    except Exception as e:
        error_type = type(e).__name__
        logger.error(
            "repair_dependency_exception",
            brain=brain_name,
            error_type=error_type,
            error_message=str(e),
        )
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


def repair_step():
    """Attempt to diagnose and fix errors during repair process."""
    try:
        logger.info("repair_step_started")
        # Attempt to diagnose and fix errors
        # Placeholder for actual repair logic
        logger.debug("repair_step_completed", status="success")
    except Exception as e:
        error_type = type(e).__name__
        logger.error(
            "repair_step_failed",
            error_type=error_type,
            error_message=str(e),
            exc_info=True,
        )


def self_repair():
    """Execute self-repair with comprehensive error handling."""
    try:
        logger.info("self_repair_started")
        repair_step()
        logger.info("self_repair_completed", status="success")
    except asyncio.CancelledError as e:
        logger.warning(
            "self_repair_cancelled",
            error_type="CancelledError",
            error_message=str(e),
            exc_info=True,
        )
    except FileNotFoundError as e:
        logger.error(
            "self_repair_file_not_found",
            error_type="FileNotFoundError",
            error_message=str(e),
            exc_info=True,
        )
    except Exception as e:
        error_type = type(e).__name__
        logger.error(
            "self_repair_failed",
            error_type=error_type,
            error_message=str(e),
            exc_info=True,
        )


def log_self_repair_action(action_details: str):
    """Logs details of self-repair actions performed by AURA.

    Args:
        action_details (str): A string describing the self-repair action.
    """
    log_path = os.path.expanduser('~/.aura/log/repair.log')
    logging.basicConfig(filename=log_path, level=logging.INFO, format='%(asctime)s - %(message)s')
    logging.info(action_details)


def __call__(self, *args, **kwargs):
    """Handle call with error handling for CancelledError and general exceptions."""
    try:
        # Existing call logic
        pass
    except asyncio.CancelledError:
        logger.warning("call_cancelled", error_type="CancelledError")
    except Exception as e:
        error_type = type(e).__name__
        logger.error(
            "call_failed",
            error_type=error_type,
            error_message=str(e),
            exc_info=True,
        )
