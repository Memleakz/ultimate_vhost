import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from rich.console import Console as _Console

_console = _Console()

_active_live: Optional[Any] = None

_ELEVATED_MESSAGE = (
    "[vhost] Elevated privileges required. You may be prompted for your password."
)

_NON_TTY_WARNING = (
    "Warning: Non-interactive terminal detected. "
    "Ensure passwordless sudo is configured for this environment."
)


def get_sudo_prefix() -> list[str]:
    """
    Returns a list of command prefix for privileged operations.
    If the current user is root (UID 0), returns an empty list.
    Otherwise, if 'sudo' is available in the system, returns ['sudo'].
    Otherwise, returns an empty list (may handle root-only environments like Docker).
    """
    if os.getuid() == 0:
        return []

    if shutil.which("sudo"):
        return ["sudo"]

    return []


def set_active_live(live: Optional[Any]) -> None:
    """Register (or clear) the currently active Rich Live/Status context.

    Call with the active Status object before entering a spinner block so that
    run_elevated_command() can stop it before spawning a privileged subprocess.
    Call with None after the spinner block exits to clear the reference.
    """
    global _active_live
    _active_live = live


def preflight_sudo_check() -> None:
    """Warm the sudo credentials cache before any spinner is started.

    Must be called before the first console.status() spinner in any command
    that will perform privileged operations.  This ensures the OS password
    prompt is always shown in a clean, spinner-free terminal window.

    Behaviour:
    - Root (UID 0) or no sudo binary: returns immediately (no-op).
    - Non-TTY stdin: prints a plain-text warning to stderr, then returns so
      that passwordless sudo setups (NOPASSWD) can still proceed.
    - Interactive TTY: runs ``sudo -v`` to validate / refresh the credentials
      cache.  Exits with code 1 and a human-readable error if authentication
      fails, before any file is written.
    """
    if os.getuid() == 0:
        return

    if not shutil.which("sudo"):
        return

    if not sys.stdin.isatty():
        sys.stderr.write(f"{_NON_TTY_WARNING}\n")
        sys.stderr.flush()
        return

    sys.stdout.flush()
    sys.stderr.flush()

    result = subprocess.run(["sudo", "-v"])

    if result.returncode != 0:
        sys.stderr.write("Error: Failed to acquire sudo privileges. Aborting.\n")
        sys.stderr.flush()
        raise SystemExit(1)


def run_elevated_command(
    cmd: list[str],
    *,
    stdin=None,
    stdout=None,
    stderr=None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """
    Run a privileged command with proper TTY passthrough for sudo password prompts.

    Before spawning the subprocess this function:
      1. Validates that stdin is not subprocess.PIPE or subprocess.DEVNULL so
         sudo can read the password directly from the terminal's TTY.
      2. Flushes sys.stdout and sys.stderr to prevent buffered Rich output from
         interleaving with the OS password prompt.
      3. Prints the prescribed pre-prompt message in yellow/bold so the user
         knows elevated input is expected.

    Args:
        cmd: The full command list, potentially prefixed with 'sudo'.
        stdin: File object or None (inherited TTY). Must NOT be subprocess.PIPE
               or subprocess.DEVNULL.
        stdout: stdout argument forwarded to subprocess.run (None = inherited).
        stderr: stderr argument forwarded to subprocess.run (None = inherited).
        check: When True, raise RuntimeError if the process exits non-zero.

    Returns:
        The completed subprocess.CompletedProcess instance.

    Raises:
        ValueError: If stdin is subprocess.PIPE or subprocess.DEVNULL.
        RuntimeError: If check=True and the process returns a non-zero exit code.
    """
    if stdin in (subprocess.PIPE, subprocess.DEVNULL):
        raise ValueError(
            "run_elevated_command does not allow stdin=subprocess.PIPE or "
            "stdin=subprocess.DEVNULL; pass a file object or None to allow TTY passthrough."
        )

    if "sudo" in cmd:
        global _active_live
        if _active_live is not None:
            _active_live.stop()
            _active_live = None

        sys.stdout.flush()
        sys.stderr.flush()
        _console.print(f"[bold yellow]{_ELEVATED_MESSAGE}[/bold yellow]")
        sys.stdout.flush()

    result = subprocess.run(cmd, stdin=stdin, stdout=stdout, stderr=stderr)

    if "sudo" in cmd and result.returncode == 0:
        _console.print("[green]✔[/green] Privileges confirmed.")

    if check and result.returncode != 0:
        raise RuntimeError(
            f"Elevated command failed: {' '.join(str(a) for a in cmd)!r} "
            f"exited with code {result.returncode}"
        )

    return result


def is_service_running(service_name: str) -> bool:
    """
    Check if a service is running using systemd (systemctl) with a fallback
    to checking the process list (pgrep).
    """
    # 1. Try systemctl (preferred)
    try:
        # systemctl is-active returns 0 if active, non-zero otherwise
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.stdout.strip() == "active":
            return True
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        pass

    # 2. Fallback: check process list via pgrep
    # This is useful for containers (Docker/Podman) or systems without systemd
    try:
        # Map service names to likely process names
        # Nginx usually has 'nginx' master/worker processes.
        # Apache can be 'apache2' (Debian) or 'httpd' (RHEL).
        proc_names = [service_name]
        if "apache" in service_name or "httpd" in service_name:
            proc_names.extend(["apache2", "httpd"])

        for name in set(proc_names):
            # pgrep -x matches the exact process name
            result = subprocess.run(
                ["pgrep", "-x", name],
                capture_output=True,
                timeout=3,
            )
            if result.returncode == 0:
                return True
    except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        pass

    return False


def reload_service(service_name: str, fallback_args: Optional[list[str]] = None) -> None:
    """
    Reloads a service using systemctl with an optional fallback CLI command.
    Example: reload_service("nginx", ["nginx", "-s", "reload"])
    """
    try:
        cmd = get_sudo_prefix() + ["systemctl", "reload", service_name]
        run_elevated_command(cmd)
    except RuntimeError as e:
        if fallback_args:
            try:
                fallback_cmd = get_sudo_prefix() + fallback_args
                run_elevated_command(fallback_cmd)
            except RuntimeError:
                raise RuntimeError(
                    f"Failed to reload {service_name} via systemctl and fallback: {e}"
                )
        else:
            raise RuntimeError(f"Failed to reload {service_name} via systemctl: {e}")


def apply_selinux_context(
    target_path: Path, context_type: str = "httpd_config_t", recursive: bool = False
) -> None:
    """
    Applies a SELinux context to a path using chcon.
    The caller is responsible for first checking if SELinux is enforcing.
    Defaults to httpd_config_t for configuration files.
    """
    cmd = get_sudo_prefix() + ["chcon", "-t", context_type]
    if recursive:
        cmd.append("-R")
    cmd.append(str(target_path))

    try:
        run_elevated_command(cmd)
    except RuntimeError as e:
        raise RuntimeError(f"SELinux context application failed for {target_path}: {e}")
