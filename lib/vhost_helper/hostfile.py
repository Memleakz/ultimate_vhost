import re
import subprocess
import tempfile
from pathlib import Path

from .config import HOSTS_FILE
from .utils import get_sudo_prefix, run_elevated_command


def add_entry(ip: str, domain: str):
    """Appends an entry to /etc/hosts if it doesn't exist."""
    entry = f"{ip}\t{domain}"

    with open(HOSTS_FILE, "r") as f:
        content = f.read()
        if re.search(
            fr"^\s*{re.escape(ip)}\s+{re.escape(domain)}(\s|$)", content, re.MULTILINE
        ):
            return

        if re.search(
            fr"^\s*[\d.]+\s+{re.escape(domain)}(\s|$)", content, re.MULTILINE
        ):
            remove_entry(domain)

    sudo_prefix = get_sudo_prefix()
    try:
        if sudo_prefix:
            # Use NamedTemporaryFile to avoid mktemp() race conditions.
            # We must close it before 'tee' reads it, or flush it.
            with tempfile.NamedTemporaryFile(mode="w", suffix=".vhosts_entry", delete=False) as tmp_file:
                tmp_file.write(f"{entry}\n")
                tmp_path = Path(tmp_file.name)
            
            try:
                cmd = sudo_prefix + ["tee", "-a", str(HOSTS_FILE)]
                with open(tmp_path, "rb") as entry_stdin:
                    run_elevated_command(cmd, stdin=entry_stdin, stdout=subprocess.DEVNULL)
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            with open(HOSTS_FILE, "a") as f:
                f.write(f"{entry}\n")
    except (RuntimeError, OSError) as e:
        raise RuntimeError(f"Failed to add hostfile entry: {e}")


def remove_entry(domain: str):
    """Removes a domain entry from /etc/hosts."""
    try:
        # Read the file
        with open(HOSTS_FILE, "r") as f:
            lines = f.readlines()
        
        # Filter lines
        pattern = re.compile(fr"(^|\s){re.escape(domain)}(\s|$)")
        new_lines = [line for line in lines if not pattern.search(line)]
        
        if len(new_lines) == len(lines):
            return

        sudo_prefix = get_sudo_prefix()
        if sudo_prefix:
            # Write new content to a temp file then tee it to /etc/hosts
            # This avoids 'sed -i' which fails when /etc/hosts is a mount point (common in containers).
            with tempfile.NamedTemporaryFile(mode="w", suffix=".vhosts_clean", delete=False) as tmp_file:
                tmp_file.writelines(new_lines)
                tmp_path = Path(tmp_file.name)
            
            try:
                cmd = sudo_prefix + ["tee", str(HOSTS_FILE)]
                with open(tmp_path, "rb") as clean_stdin:
                    # Overwrite the file (no -a)
                    run_elevated_command(cmd, stdin=clean_stdin, stdout=subprocess.DEVNULL)
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            with open(HOSTS_FILE, "w") as f:
                f.writelines(new_lines)
    except Exception as e:
        raise RuntimeError(f"Failed to remove hostfile entry: {e}")
