"""PHP-FPM socket discovery and service orchestration."""

import glob
import os
import shutil
import subprocess
import re
from typing import Optional


class PhpFpmNotFoundError(Exception):
    """Raised when the requested PHP-FPM version cannot be located on the system."""


def resolve_socket_path(version: str, os_family: str) -> str:
    """Return the expected PHP-FPM Unix socket path for a given version and OS family.

    Args:
        version: Dotted version string, e.g. ``"8.2"``.
        os_family: OS family string, e.g. ``"debian_family"`` or ``"rhel_family"``.

    Returns:
        Absolute path to the expected socket file.
    """
    if os_family == "rhel_family":
        return "/run/php-fpm/www.sock"
    return f"/run/php/php{version}-fpm.sock"


def get_service_name(version: str, os_family: str) -> str:
    """Return the systemd service name for PHP-FPM.

    Args:
        version: Dotted version string, e.g. ``"8.2"``.
        os_family: OS family string, e.g. ``"debian_family"`` or ``"rhel_family"``.

    Returns:
        Service name string suitable for passing to ``systemctl``.
    """
    if os_family == "rhel_family":
        return "php-fpm"
    return f"php{version}-fpm"


def _parse_php_version_from_output(output: str) -> Optional[str]:
    """Extract a dotted version string from ``php --version`` output.

    Args:
        output: Raw stdout string from ``php --version``.

    Returns:
        Version string (e.g. ``"8.2"``) or ``None`` if parsing fails.
    """
    match = re.search(r"PHP\s+(\d+\.\d+)", output)
    if match:
        return match.group(1)
    return None


def detect_default_version(os_family: str) -> str:
    """Auto-detect the highest installed PHP-FPM version.

    For Debian/Ubuntu, scans ``/run/php/php*-fpm.sock`` and selects the
    highest version found. Also probes ``shutil.which("php")`` and parses
    ``php --version`` as an additional candidate.

    For RHEL/Fedora, verifies ``/run/php-fpm/www.sock`` exists or that the
    ``php-fpm`` binary is on PATH, then returns a sentinel version string
    (``"system"``).

    Args:
        os_family: OS family string.

    Returns:
        Dotted version string (e.g. ``"8.2"``) or ``"system"`` for RHEL.

    Raises:
        PhpFpmNotFoundError: When no PHP-FPM installation can be detected.
    """
    if os_family == "rhel_family":
        sock = "/run/php-fpm/www.sock"
        if os.path.exists(sock) or shutil.which("php-fpm") is not None:
            return "system"
        raise PhpFpmNotFoundError(
            f"PHP-FPM not found on this RHEL/Fedora system. "
            f"Expected socket: {sock}. "
            f"Install php-fpm and ensure the service has started at least once."
        )

    # Debian/Ubuntu: glob for versioned sockets
    candidates: list[str] = []

    sock_pattern = "/run/php/php*-fpm.sock"
    for sock_path in glob.glob(sock_pattern):
        m = re.search(r"php(\d+\.\d+)-fpm\.sock", sock_path)
        if m:
            candidates.append(m.group(1))

    # Also probe the active ``php`` binary
    if shutil.which("php") is not None:
        try:
            result = subprocess.run(
                ["php", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            parsed = _parse_php_version_from_output(result.stdout)
            if parsed and parsed not in candidates:
                candidates.append(parsed)
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not candidates:
        raise PhpFpmNotFoundError(
            "No PHP-FPM installation detected on this Debian/Ubuntu system. "
            f"No sockets matched '{sock_pattern}' and 'php' binary not found. "
            "Install php-fpm (e.g. sudo apt install php-fpm) and retry."
        )

    # Sort by version tuple and return highest
    def _version_key(v: str):
        try:
            return tuple(int(x) for x in v.split("."))
        except ValueError:
            return (0,)

    return max(candidates, key=_version_key)


def validate_version_present(version: str, os_family: str) -> str:
    """Verify a specific PHP-FPM version is available and return its socket path.

    Checks that either:
    * the expected socket file exists on disk, OR
    * the corresponding ``php{VERSION}-fpm`` binary is on ``PATH``.

    Args:
        version: Dotted version string, e.g. ``"8.2"``.
        os_family: OS family string.

    Returns:
        Resolved socket path string.

    Raises:
        PhpFpmNotFoundError: If neither socket nor binary is found.
    """
    socket_path = resolve_socket_path(version, os_family)
    if os_family == "rhel_family":
        if os.path.exists(socket_path) or shutil.which("php-fpm") is not None:
            return socket_path
        raise PhpFpmNotFoundError(
            f"PHP-FPM version '{version}' not found on this RHEL/Fedora system. "
            f"Expected socket: {socket_path}. "
            "Install php-fpm and ensure the service has started at least once."
        )

    # Debian: check socket path first, then binary
    binary_name = f"php{version}-fpm"
    if os.path.exists(socket_path) or shutil.which(binary_name) is not None:
        return socket_path

    raise PhpFpmNotFoundError(
        f"PHP-FPM version '{version}' not found. "
        f"Expected socket: {socket_path}. "
        f"Binary '{binary_name}' not found in PATH. "
        f"Install with: sudo apt install php{version}-fpm"
    )


def start_service(version: str, os_family: str) -> Optional[str]:
    """Attempt to enable and start the PHP-FPM service.

    This operation is **non-blocking**: failure returns a warning message
    rather than raising an exception.

    Args:
        version: Dotted version string, e.g. ``"8.2"``.
        os_family: OS family string.

    Returns:
        ``None`` on success, or a warning message string on failure.
    """
    service = get_service_name(version, os_family)
    try:
        result = subprocess.run(
            ["systemctl", "enable", "--now", service],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return (
                f"systemctl enable --now {service} exited with code {result.returncode}. "
                f"PHP-FPM may not be running. Check service logs: "
                f"journalctl -u {service}"
            )
        return None
    except FileNotFoundError:
        return (
            f"'systemctl' not found. Cannot start '{service}' automatically. "
            "Start the PHP-FPM service manually before using this vhost."
        )
    except OSError as exc:
        return f"Failed to start '{service}': {exc}"
