import os
import shutil
import subprocess
from pathlib import Path

from .models import ServerType
from .utils import get_sudo_prefix, run_elevated_command


# Authoritative resolution table: (os_family, server_type) → (user, group)
_WEBSERVER_USER_TABLE: dict[tuple[str, str], tuple[str, str]] = {
    ("debian_family", ServerType.NGINX): ("www-data", "www-data"),
    ("debian_family", ServerType.APACHE): ("www-data", "www-data"),
    ("rhel_family", ServerType.NGINX): ("nginx", "nginx"),
    ("rhel_family", ServerType.APACHE): ("apache", "apache"),
    # 'unknown' family falls through to the fallback below
}
_FALLBACK_WEBSERVER_USER: tuple[str, str] = ("www-data", "www-data")


def resolve_webserver_user_group(
    os_family: str, server_type: ServerType
) -> tuple[str, str]:
    """
    Returns the canonical (user, group) tuple for the active web server
    based on the distribution family and provider.

    Falls back to ('www-data', 'www-data') for unknown OS families.
    """
    return _WEBSERVER_USER_TABLE.get(
        (os_family, server_type), _FALLBACK_WEBSERVER_USER
    )


def get_current_user() -> str:
    """
    Returns the name of the currently logged-in user.

    Tries, in order:
    1. The SUDO_USER environment variable (set by sudo — identifies the real
       invoking user when the process is running with elevated privileges).
    2. The USER environment variable.
    3. The LOGNAME environment variable.
    4. os.getlogin().
    5. Defaults to 'root' if all methods fail (e.g., in some containers).
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        return sudo_user
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if user:
        return user
    try:
        return os.getlogin()
    except OSError:
        return "root"


def validate_webroot_perms(perms: str) -> tuple[str, str]:
    """
    Validates and parses the --webroot-perms string.

    Expected format: "<dir_mode>:<file_mode>" (e.g. "755:644" or "750:640").
    Both components must be three-digit octal strings (digits 0–7 only).

    Returns:
        A (dir_mode, file_mode) tuple of validated strings.

    Raises:
        ValueError: If the format is invalid or modes contain non-octal digits.
    """
    import re

    if not re.match(r"^\d{3}:\d{3}$", perms):
        raise ValueError(
            f"Invalid --webroot-perms format: '{perms}'. "
            "Expected '<dir_mode>:<file_mode>' (e.g. '755:644')."
        )

    dir_mode, file_mode = perms.split(":")

    for char in dir_mode + file_mode:
        if char not in "01234567":
            raise ValueError(
                f"Invalid --webroot-perms value: '{perms}'. "
                "Both modes must contain only octal digits (0–7)."
            )

    return dir_mode, file_mode


def validate_unix_name(name: str, label: str) -> str:
    """
    Validates a Unix username or group name.

    Accepts names that start with a letter or underscore, followed by
    alphanumeric characters, hyphens, underscores, or dots — matching
    the POSIX portable filename character set for login names.

    Raises:
        ValueError: If the name contains characters that would break the
                    chown '<user>:<group>' argument (e.g. colons, spaces,
                    newlines) or is empty.
    """
    import re

    if not name:
        raise ValueError(f"{label} must not be empty.")
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_\-\.]*$", name):
        raise ValueError(
            f"Invalid {label}: '{name}'. "
            "Must start with a letter or underscore and contain only "
            "alphanumeric characters, hyphens, underscores, or dots."
        )
    return name


def apply_webroot_permissions(
    path: Path,
    user: str,
    group: str,
    dir_mode: str = "755",
    file_mode: str = "644",
) -> None:
    """
    Applies the "gold standard" POSIX ownership and permission model to a webroot.

    Executes exactly four privileged commands in order:
    1. chown -R <user>:<group> <path>
    2. find <path> -type d -exec chmod <dir_mode> {} +
    3. find <path> -type f -exec chmod <file_mode> {} +
    4. find <path> -type d -exec chmod g+s {} +   (SetGID — group-inheriting directories)

    Raises:
        RuntimeError: If any elevated command exits non-zero.
    """
    sudo = get_sudo_prefix()
    path_str = str(path)

    run_elevated_command(sudo + ["chown", "-R", f"{user}:{group}", path_str])
    run_elevated_command(
        sudo + ["find", path_str, "-type", "d", "-exec", "chmod", dir_mode, "{}", "+"]
    )
    run_elevated_command(
        sudo + ["find", path_str, "-type", "f", "-exec", "chmod", file_mode, "{}", "+"]
    )
    run_elevated_command(
        sudo + ["find", path_str, "-type", "d", "-exec", "chmod", "g+s", "{}", "+"]
    )


def is_selinux_active() -> bool:
    """
    Returns True if SELinux is installed and in Enforcing OR Permissive mode.

    Unlike is_selinux_enforcing() (which gates config-file labelling), this
    function is used to gate webroot content labelling where both modes warrant
    applying the httpd_sys_content_t context.
    """
    if not shutil.which("getenforce"):
        return False

    try:
        result = subprocess.run(["getenforce"], capture_output=True, text=True)
        if not result.stdout:
            return False
        return result.stdout.strip() in ("Enforcing", "Permissive")
    except Exception:
        return False


def apply_selinux_webroot_context(path: Path) -> None:
    """
    Applies the httpd_sys_content_t SELinux context to a webroot path.

    Preferred path (persistent):
        semanage fcontext -a -t httpd_sys_content_t "<path>(/.*)?
        restorecon -Rv <path>

    Fallback (non-persistent, when semanage is not available):
        chcon -Rt httpd_sys_content_t <path>

    If semanage is present but fails, falls through to the chcon fallback.
    If chcon also fails, raises RuntimeError (triggers full rollback in caller).

    Raises:
        RuntimeError: If the chcon fallback fails.
    """
    sudo = get_sudo_prefix()
    path_str = str(path)

    if shutil.which("semanage"):
        try:
            run_elevated_command(
                sudo
                + [
                    "semanage",
                    "fcontext",
                    "-a",
                    "-t",
                    "httpd_sys_content_t",
                    f"{path_str}(/.*)?",
                ]
            )
            run_elevated_command(sudo + ["restorecon", "-Rv", path_str])
            return
        except RuntimeError:
            # semanage or restorecon failed — fall through to chcon
            pass

    chcon_cmd = sudo + ["chcon", "-Rt", "httpd_sys_content_t", path_str]
    try:
        run_elevated_command(chcon_cmd)
    except RuntimeError as exc:
        raise RuntimeError(
            f"SELinux context application failed for '{path_str}'. "
            f"Both semanage and chcon approaches failed: {exc}"
        )
