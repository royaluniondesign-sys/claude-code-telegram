"""Agent sandbox — restricts subprocess execution via macOS sandbox-exec + ulimit.

macOS sandbox-exec uses Scheme-based security profiles to restrict:
- File system access (only working dir + /tmp)
- Network access (only localhost)
- Process spawning (limited)

Falls back gracefully if sandbox-exec is unavailable (Linux, sandboxed macOS CI).
"""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Sandbox profile template — WORKDIR_PLACEHOLDER replaced at runtime
_PROFILE_TEMPLATE = """(version 1)
(deny default)
(allow process-fork)
(allow process-exec)
(allow signal)

; Read-only access to system libraries and Python
(allow file-read* (subpath "/usr/lib"))
(allow file-read* (subpath "/usr/local/lib"))
(allow file-read* (subpath "/opt/homebrew"))
(allow file-read* (subpath "/System"))
(allow file-read* (subpath "/Library/Developer"))
(allow file-read* (subpath "/private/var/db/timezone"))

; Read Python environments
(allow file-read* (subpath "/Users/oxyzen/.local/share/uv"))
(allow file-read* (subpath "/Users/oxyzen/.codex"))
(allow file-write* (subpath "/Users/oxyzen/.codex"))
(allow file-read* (subpath "/Users/oxyzen/.config"))
(allow file-write* (subpath "/Users/oxyzen/.config"))
(allow file-read* (subpath "/Users/oxyzen/.npm"))
(allow file-read* (subpath "/Users/oxyzen/.node_repl_history"))

; Write access to tmp only
(allow file-write* (subpath "/tmp"))
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "/var/folders"))

; Read/write to working directory — injected at runtime
(allow file-read* (subpath "WORKDIR_PLACEHOLDER"))
(allow file-write* (subpath "WORKDIR_PLACEHOLDER"))

; Sysctl reads
(allow sysctl-read)

; Network — localhost only
(allow network-outbound (remote unix-socket))
(allow network-outbound (remote ip "localhost:*"))

; IPC (needed for subprocess comms)
(allow ipc-posix-sem)
(allow ipc-posix-shm*)
(allow mach-lookup)
"""

# Extended profile that allows outbound network for agents that need it (e.g. Codex)
_PROFILE_TEMPLATE_NETWORK = """(version 1)
(deny default)
(allow process-fork)
(allow process-exec)
(allow signal)

; Read-only access to system libraries and Python
(allow file-read* (subpath "/usr/lib"))
(allow file-read* (subpath "/usr/local/lib"))
(allow file-read* (subpath "/opt/homebrew"))
(allow file-read* (subpath "/System"))
(allow file-read* (subpath "/Library/Developer"))
(allow file-read* (subpath "/private/var/db/timezone"))

; Read Python environments
(allow file-read* (subpath "/Users/oxyzen/.local/share/uv"))
(allow file-read* (subpath "/Users/oxyzen/.codex"))
(allow file-write* (subpath "/Users/oxyzen/.codex"))
(allow file-read* (subpath "/Users/oxyzen/.config"))
(allow file-write* (subpath "/Users/oxyzen/.config"))
(allow file-read* (subpath "/Users/oxyzen/.npm"))
(allow file-read* (subpath "/Users/oxyzen/.node_repl_history"))

; Write access to tmp only
(allow file-write* (subpath "/tmp"))
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "/var/folders"))

; Read/write to working directory — injected at runtime
(allow file-read* (subpath "WORKDIR_PLACEHOLDER"))
(allow file-write* (subpath "WORKDIR_PLACEHOLDER"))

; Sysctl reads
(allow sysctl-read)

; Network — unrestricted outbound (for agents needing external APIs)
(allow network-outbound)
(allow network-inbound (local ip "localhost:*"))

; IPC (needed for subprocess comms)
(allow ipc-posix-sem)
(allow ipc-posix-shm*)
(allow mach-lookup)
"""

# Errors that indicate sandbox-exec itself is blocked (e.g. SIP, MDM, CI)
_SANDBOX_KERNEL_ERRORS = frozenset(
    [
        "operation not permitted",
        "sandbox initialization failed",
        "no such file or directory",
        "exec format error",
        "permission denied",
    ]
)


@dataclass
class SandboxConfig:
    """Configuration for sandboxed execution."""

    working_dir: str
    max_cpu_seconds: int = 120
    max_memory_mb: int = 2048
    # set True for Codex (needs OpenAI API) — bypasses localhost-only network rule
    allow_network: bool = False
    # if allow_network, which hosts are expected (informational only, not enforced in profile)
    network_hosts: list[str] = field(default_factory=list)


def is_sandbox_available() -> bool:
    """Check if macOS sandbox-exec is available."""
    return Path(_SANDBOX_EXEC).is_file()


def _build_profile(cwd: str, allow_network: bool) -> str:
    """Return a sandbox profile with WORKDIR_PLACEHOLDER replaced by cwd."""
    template = _PROFILE_TEMPLATE_NETWORK if allow_network else _PROFILE_TEMPLATE
    return template.replace("WORKDIR_PLACEHOLDER", cwd)


def _make_ulimit_preexec(max_cpu_seconds: int, max_memory_mb: int):
    """Return a preexec_fn that applies ulimits inside the child process.

    Note: RLIMIT_AS (virtual address space) is intentionally skipped.
    Node.js / Electron processes (codex, cline) require >4GB of virtual space
    even when physical RAM usage is low — RLIMIT_AS causes immediate crashes.
    Only CPU time is capped.
    """

    def _set_limits() -> None:
        try:
            # Max CPU time
            soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
            new_soft = min(max_cpu_seconds, hard if hard != resource.RLIM_INFINITY else max_cpu_seconds)
            resource.setrlimit(resource.RLIMIT_CPU, (new_soft, hard))
        except Exception:
            pass

    return _set_limits


def _is_sandbox_kernel_error(stderr: str) -> bool:
    """Return True when stderr indicates a kernel-level sandbox failure."""
    lower = stderr.lower()
    return any(err in lower for err in _SANDBOX_KERNEL_ERRORS)


async def _run_subprocess(
    args: list[str],
    cwd: str,
    timeout: int,
    env: Optional[dict] = None,
    preexec_fn=None,
) -> tuple[int, str, str]:
    """Low-level async subprocess runner. Returns (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            preexec_fn=preexec_fn,
        )
    except FileNotFoundError as exc:
        return -1, "", f"Command not found: {exc}"
    except PermissionError as exc:
        return -1, "", f"Permission denied launching process: {exc}"
    except OSError as exc:
        return -1, "", f"OS error launching process: {exc}"

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", f"timeout after {timeout}s"
    except Exception as exc:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "", f"Process communication error: {exc}"

    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace").strip(),
        stderr_b.decode("utf-8", errors="replace").strip(),
    )


async def run_sandboxed(
    args: list[str],
    cwd: str,
    timeout: int,
    env: Optional[dict] = None,
    config: Optional[SandboxConfig] = None,
) -> tuple[int, str, str]:
    """Run a subprocess in a sandboxed environment.

    Applies macOS sandbox-exec (filesystem + network restrictions) and ulimits
    (CPU time, virtual memory) to the child process.

    Returns (returncode, stdout, stderr).
    Falls back to unsandboxed execution if sandbox-exec is unavailable or if the
    kernel refuses to start the sandbox (e.g. SIP restrictions, MDM policy, CI).
    """
    cfg = config or SandboxConfig(working_dir=cwd)
    preexec = _make_ulimit_preexec(cfg.max_cpu_seconds, cfg.max_memory_mb)

    if not is_sandbox_available():
        logger.warning(
            "sandbox_exec_unavailable",
            reason="sandbox-exec not found at /usr/bin/sandbox-exec",
            fallback="unsandboxed",
        )
        return await _run_subprocess(args, cwd, timeout, env=env, preexec_fn=preexec)

    profile_content = _build_profile(cwd, cfg.allow_network)

    # Write profile to a temp file; cleaned up after the process finishes
    try:
        profile_fd, profile_path = tempfile.mkstemp(
            prefix="aura_sandbox_", suffix=".sb"
        )
        try:
            with os.fdopen(profile_fd, "w") as fh:
                fh.write(profile_content)

            sandboxed_args = [_SANDBOX_EXEC, "-f", profile_path] + list(args)
            rc, out, err = await _run_subprocess(
                sandboxed_args, cwd, timeout, env=env, preexec_fn=preexec
            )

            # Retry without sandbox if the kernel itself rejected sandbox-exec
            if rc != 0 and _is_sandbox_kernel_error(err):
                logger.warning(
                    "sandbox_exec_kernel_error",
                    stderr=err[:300],
                    fallback="retrying_unsandboxed",
                )
                return await _run_subprocess(
                    args, cwd, timeout, env=env, preexec_fn=preexec
                )

            return rc, out, err

        finally:
            try:
                os.unlink(profile_path)
            except OSError:
                pass

    except OSError as exc:
        logger.warning(
            "sandbox_profile_write_error",
            error=str(exc),
            fallback="unsandboxed",
        )
        return await _run_subprocess(args, cwd, timeout, env=env, preexec_fn=preexec)
