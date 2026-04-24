import subprocess
import os
import shutil
from pathlib import Path
from .models import OSInfo

# Canonical OS IDs per family
_DEBIAN_IDS = frozenset({"debian", "ubuntu", "linuxmint", "pop", "elementary", "raspbian", "kali"})
_RHEL_IDS = frozenset({"rhel", "centos", "fedora", "almalinux", "rocky", "ol", "amzn"})


def detect_os_family(os_release_path: str = "/etc/os-release") -> str:
    """
    Reads /etc/os-release to classify the host OS into a canonical family.

    Checks both the ID and ID_LIKE fields for comprehensive detection.

    Returns:
        'debian_family', 'rhel_family', or 'unknown'.
        Falls back to 'unknown' if the file cannot be read or the OS is unrecognised.
    """
    release_file = Path(os_release_path)
    if not release_file.exists():
        return "unknown"

    try:
        fields: dict[str, str] = {}
        with open(release_file, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                fields[key.strip()] = value.strip().strip('"').strip("'")

        os_id = fields.get("ID", "").lower()
        id_like_tokens = set(fields.get("ID_LIKE", "").lower().split())

        if os_id in _DEBIAN_IDS or _DEBIAN_IDS & id_like_tokens:
            return "debian_family"
        if os_id in _RHEL_IDS or _RHEL_IDS & id_like_tokens:
            return "rhel_family"

        return "unknown"
    except (OSError, IOError):
        return "unknown"


def get_os_info() -> OSInfo:
    """Executes bin/detect_os.sh and returns parsed OS data."""
    # Try to find detect_os.sh relative to project root
    script_path = Path(__file__).parent.parent.parent / "bin" / "detect_os.sh"
    
    if not script_path.exists():
        raise FileNotFoundError(f"OS detection script not found at {script_path}")

    try:
        result = subprocess.run([str(script_path)], capture_output=True, text=True, check=True)
        lines = result.stdout.strip().split('\n')
        
        info = {}
        for line in lines:
            if '=' in line:
                key, value = line.split('=', 1)
                info[key.strip()] = value.strip()

        os_id = info.get('ID', 'unknown')
        
        # Determine OS family
        family = "unknown"
        if os_id in ["ubuntu", "debian"]:
            family = "debian"
        elif os_id in ["centos", "rhel", "fedora"]:
            family = "rhel"
        elif os_id in ["arch"]:
            family = "arch"

        return OSInfo(
            id=os_id,
            version=info.get('VERSION', 'unknown'),
            family=family
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"OS detection failed: {e.stderr}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error during OS detection: {e}")


def is_selinux_enforcing() -> bool:
    """
    Checks if SELinux is installed and in an 'Enforcing' state.

    Returns:
        True if `getenforce` command exists and returns 'Enforcing', False otherwise.
    """
    if not shutil.which("getenforce"):
        return False
    
    try:
        result = subprocess.run(["getenforce"], capture_output=True, text=True)
        # Defensive check for poorly mocked subprocess.run in other tests
        if not result.stdout:
            return False
        return result.stdout.strip() == "Enforcing"
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
