import subprocess
import shutil
import tempfile
import re
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, TemplateNotFound
from typing import List, Optional, Tuple, Dict

from ..models import VHostConfig, VHostInfo, ServerType
from ..config import (
    APACHE_SITES_AVAILABLE,
    APACHE_SITES_ENABLED,
    APACHE_SITES_DISABLED,
    APACHE_SERVICE_NAME,
    detected_os_family,
    APP_TEMPLATES_DIR,
    USER_TEMPLATES_DIR,
)
from ..utils import (
    get_sudo_prefix,
    run_elevated_command,
    is_service_running,
)
from ..os_detector import is_selinux_enforcing


def is_apache_installed() -> bool:
    """Returns True if the apache2 or httpd binary is discoverable in the system PATH."""
    return shutil.which("apache2") is not None or shutil.which("httpd") is not None


def is_apache_running() -> bool:
    """
    Returns True if the apache service is running.
    Uses systemd with a pgrep fallback for non-systemd environments.
    """
    return is_service_running(APACHE_SERVICE_NAME)


def _extract_apache_vhost_details(
    config_content: str, config_path: Path
) -> Tuple[Optional[str], Optional[Path]]:
    """
    Extracts the domain (ServerName) and document root from Apache config content.
    Returns (domain, document_root) or (None, None) if not found.
    """
    domain = None
    document_root = None

    # Find ServerName directive, which can be followed by a port or just the domain.
    server_name_match = re.search(
        r"ServerName\s+([^\s:]+)", config_content, re.IGNORECASE
    )
    if server_name_match:
        domain = server_name_match.group(1).strip()

    # Find DocumentRoot directive.
    document_root_match = re.search(
        r"DocumentRoot\s+([^\s]+)", config_content, re.IGNORECASE
    )
    if document_root_match:
        # Remove quotes if present
        root_path_str = document_root_match.group(1).strip().strip("\"'")
        document_root = Path(root_path_str)

    # Fallback if domain is not found, use filename
    if (
        domain is None and config_path.stem != "000-default"
    ):  # Avoid using '000-default' as a domain
        domain = config_path.stem
        if "external-" in domain:
            domain = domain.replace("external-", "")

    return domain, document_root


class ApacheProvider:
    def __init__(self):
        # Set up search paths for templates, user's first
        user_template_path = USER_TEMPLATES_DIR / "apache"
        app_template_path = APP_TEMPLATES_DIR / "apache"

        # ChoiceLoader tries loaders in order until one finds a template
        self.env = Environment(
            loader=ChoiceLoader(
                [
                    FileSystemLoader(str(user_template_path)),
                    FileSystemLoader(str(app_template_path)),
                ]
            ),
            autoescape=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.os_family = detected_os_family

    def _get_template(self, template_name: str):
        """
        Finds a template by searching user and then app directories.
        """
        template_filename = f"{template_name}.conf.j2"
        try:
            return self.env.get_template(template_filename)
        except TemplateNotFound:
            # Raise a more explicit error with search paths
            user_path = USER_TEMPLATES_DIR / "apache" / template_filename
            app_path = APP_TEMPLATES_DIR / "apache" / template_filename
            raise FileNotFoundError(
                f"Template '{template_name}' not found for Apache. Searched in:\n"
                f"1. {user_path}\n"
                f"2. {app_path}"
            )

    def list_all_vhosts(self) -> List[VHostInfo]:
        """
        Discovers and returns a list of all Apache virtual hosts on the system.
        This method scans sites-available, sites-enabled, and sites-disabled
        (for RHEL), distinguishes between managed and external configurations
        by looking for a signature, and correctly determines the status.
        """
        vhosts_map: Dict[Path, VHostInfo] = {}
        scan_dirs = {APACHE_SITES_AVAILABLE, APACHE_SITES_ENABLED}
        if (
            self.os_family == "rhel_family"
            and APACHE_SITES_DISABLED
            and APACHE_SITES_DISABLED.exists()
        ):
            scan_dirs.add(APACHE_SITES_DISABLED)

        # 1. Get a set of real paths for all enabled configurations
        enabled_paths = set()
        if APACHE_SITES_ENABLED.exists():
            for path in APACHE_SITES_ENABLED.iterdir():
                if path.is_file() or path.is_symlink():
                    try:
                        enabled_paths.add(path.resolve())
                    except FileNotFoundError:
                        continue  # Ignore broken symlinks

        # 2. Iterate through all relevant directories to find config files
        for directory in scan_dirs:
            if not directory.exists():
                continue

            for config_path in directory.iterdir():
                if not config_path.name.endswith(".conf"):
                    continue

                try:
                    real_path = config_path.resolve()
                    if not real_path.is_file() or real_path in vhosts_map:
                        continue

                    content = real_path.read_text()
                    domain, doc_root = _extract_apache_vhost_details(content, real_path)

                    if not domain:
                        continue

                    is_managed = "# Generated by VHost Helper" in content
                    status = "Enabled" if real_path in enabled_paths else "Disabled"

                    vhosts_map[real_path] = VHostInfo(
                        domain=domain,
                        config_path=real_path,
                        server_type=ServerType.APACHE,
                        status=status,
                        managed_by="VHost Helper" if is_managed else "External",
                        document_root=doc_root,
                    )

                except (PermissionError, OSError, FileNotFoundError):
                    continue  # Skip files we can't read or access

        return list(vhosts_map.values())

    def create_vhost(self, config: VHostConfig, service_running: bool = True):
        """
        Generates and enables Apache configuration files.
        """
        template = self._get_template(config.template)
        rendered_config = template.render(
            domain=config.domain,
            document_root=str(config.document_root),
            port=config.port,
            runtime=config.runtime.value,
            python_port=config.python_port,
            node_port=config.node_port,
            node_socket=config.node_socket,
            php_socket=config.php_socket,
            os_family=self.os_family,
            use_ssl=config.ssl_enabled,
            cert_path=str(config.cert_path) if config.cert_path else "",
            key_path=str(config.key_path) if config.key_path else "",
        )

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".conf"
        ) as temp_file:
            temp_file.write(rendered_config)
            temp_file_path = Path(temp_file.name)

        try:
            # For RHEL, config_path is the enabled path. For Debian, it's the available path.
            config_path = APACHE_SITES_AVAILABLE / (config.domain + ".conf")
            cmd_mv = get_sudo_prefix() + ["mv", str(temp_file_path), str(config_path)]
            run_elevated_command(cmd_mv)

            cmd_chmod = get_sudo_prefix() + ["chmod", "644", str(config_path)]
            run_elevated_command(cmd_chmod)

            if is_selinux_enforcing():
                try:
                    cmd_chcon = get_sudo_prefix() + [
                        "chcon",
                        "-t",
                        "httpd_config_t",
                        str(config_path),
                    ]
                    run_elevated_command(cmd_chcon)
                except RuntimeError:
                    self.remove_vhost(config.domain, service_running=False)
                    raise RuntimeError(
                        f"Failed to apply SELinux context. Rollback complete. "
                        f"To set manually: sudo chcon -t httpd_config_t {config_path}"
                    )

            # Debian-specific: create symbolic link
            if self.os_family == "debian_family":
                enabled_link = APACHE_SITES_ENABLED / (config.domain + ".conf")
                if not enabled_link.exists():
                    cmd_ln = get_sudo_prefix() + [
                        "ln",
                        "-s",
                        str(config_path),
                        str(enabled_link),
                    ]
                    run_elevated_command(cmd_ln)

            if service_running:
                if self.validate_config():
                    try:
                        self.reload()
                    except Exception as reload_error:
                        self.remove_vhost(config.domain)
                        raise RuntimeError(
                            f"Apache reload failed, rollback performed: {reload_error}"
                        )
                else:
                    self.remove_vhost(config.domain)
                    raise RuntimeError(
                        "Apache configuration validation failed. Rollback complete."
                    )
        except Exception as e:
            if temp_file_path.exists():
                temp_file_path.unlink()
            if isinstance(e, RuntimeError) and (
                "rollback" in str(e).lower() or "validation failed" in str(e).lower()
            ):
                raise
            raise RuntimeError(f"Failed to create Apache vhost: {e}")

    def remove_vhost(self, domain: str, service_running: bool = True):
        """
        Deletes Apache configuration files from all possible locations.
        """
        config_paths_to_check = [
            APACHE_SITES_AVAILABLE / (domain + ".conf"),
            APACHE_SITES_ENABLED / (domain + ".conf"),
        ]
        if self.os_family == "rhel_family" and APACHE_SITES_DISABLED:
            config_paths_to_check.append(APACHE_SITES_DISABLED / (domain + ".conf"))

        # Use a set to avoid trying to remove the same file twice (e.g. RHEL)
        unique_paths = {
            p for p in config_paths_to_check if p.exists() or p.is_symlink()
        }

        try:
            for path in unique_paths:
                cmd_rm = get_sudo_prefix() + ["rm", str(path)]
                run_elevated_command(cmd_rm)

            if service_running:
                self.reload()
        except (RuntimeError, subprocess.CalledProcessError) as e:
            raise RuntimeError(f"Failed to remove Apache vhost: {e}")

    def enable_vhost(self, config_file_path, service_running: bool = True):
        """
        Enables a virtual host.
        - Debian: Creates a symbolic link.
        - RHEL: Moves the file from 'conf.disabled' to 'conf.d'.

        Args:
            config_file_path: Either a Path to the config file, or a domain name string.
                              When a string is given, the config path is resolved from the
                              standard sites-available directory.
        """
        if isinstance(config_file_path, str):
            domain = config_file_path
            config_file_path = APACHE_SITES_AVAILABLE / (domain + ".conf")
        else:
            domain = config_file_path.stem

        if self.os_family == "rhel_family":
            if not APACHE_SITES_DISABLED:
                raise RuntimeError("APACHE_SITES_DISABLED path is not configured.")
            # For RHEL, config_file_path is expected to be in APACHE_SITES_DISABLED
            # or directly in APACHE_SITES_AVAILABLE if it's a new external file.
            # We assume config_file_path points to the 'available' version or similar.
            enabled_config_target = APACHE_SITES_ENABLED / (domain + ".conf")

            # If the config is in 'sites-available' (or an external location)
            # and needs to be moved to 'sites-enabled' (RHEL style).
            # If it's already in sites-enabled, we don't need to do anything.
            if config_file_path.parent == APACHE_SITES_AVAILABLE:
                source_path = config_file_path
            elif config_file_path.parent == APACHE_SITES_DISABLED:
                source_path = config_file_path
            else:
                # Assume it's an external file, we'll try to move it
                source_path = config_file_path

            if not source_path.exists():
                raise FileNotFoundError(
                    f"Configuration file not found at {source_path}."
                )
            if enabled_config_target.exists():
                return  # Already enabled

            cmd_mv = get_sudo_prefix() + [
                "mv",
                str(source_path),
                str(enabled_config_target),
            ]
            run_elevated_command(cmd_mv)

        else:  # Debian family
            # For Debian, config_file_path is expected to be in APACHE_SITES_AVAILABLE
            # or an external location. We create a symlink to it in APACHE_SITES_ENABLED.
            enabled_link = APACHE_SITES_ENABLED / (domain + ".conf")

            if not config_file_path.exists():
                raise FileNotFoundError(
                    f"Configuration file not found at {config_file_path}."
                )
            link_exists = enabled_link.exists()
            if (
                link_exists
                and enabled_link.is_symlink()
                and enabled_link.resolve() == config_file_path
            ):
                return  # Already enabled and correctly linked
            elif link_exists:
                # If a file or broken symlink exists, remove it before creating a new one
                run_elevated_command(
                    get_sudo_prefix() + ["rm", "-f", str(enabled_link)]
                )

            cmd_ln = get_sudo_prefix() + [
                "ln",
                "-s",
                str(config_file_path),
                str(enabled_link),
            ]
            run_elevated_command(cmd_ln)

        if service_running:
            self.reload()

    def disable_vhost(self, config_file_path, service_running: bool = True):
        """
        Disables a virtual host.
        - Debian: Removes the symbolic link.
        - RHEL: Moves the file from 'conf.d' to 'conf.disabled'.

        Args:
            config_file_path: Either a Path to the config file, or a domain name string.
                              When a string is given, the config path is resolved from the
                              standard sites-available directory.
        """
        if isinstance(config_file_path, str):
            domain = config_file_path
            config_file_path = APACHE_SITES_AVAILABLE / (domain + ".conf")
        else:
            domain = config_file_path.stem

        if self.os_family == "rhel_family":
            if not APACHE_SITES_DISABLED:
                raise RuntimeError("APACHE_SITES_DISABLED path is not configured.")

            # Ensure the conf.disabled directory exists
            if not APACHE_SITES_DISABLED.exists():
                cmd_mkdir = get_sudo_prefix() + [
                    "mkdir",
                    "-p",
                    str(APACHE_SITES_DISABLED),
                ]
                run_elevated_command(cmd_mkdir)

            # For RHEL, config_file_path is expected to be in APACHE_SITES_ENABLED
            # or an external location. We move it to APACHE_SITES_DISABLED.
            enabled_config_path = APACHE_SITES_ENABLED / (domain + ".conf")
            disabled_config_path = APACHE_SITES_DISABLED / (domain + ".conf")

            source_path = enabled_config_path  # Assume the enabled version is the one we want to disable

            if not source_path.exists():
                return  # Already disabled or doesn't exist in enabled

            cmd_mv = get_sudo_prefix() + [
                "mv",
                str(source_path),
                str(disabled_config_path),
            ]
            run_elevated_command(cmd_mv)

        else:  # Debian family
            # For Debian, we remove the symlink from APACHE_SITES_ENABLED.
            enabled_link = APACHE_SITES_ENABLED / (domain + ".conf")

            if not enabled_link.exists() and not enabled_link.is_symlink():
                return  # Already disabled or link doesn't exist

            cmd_rm_link = get_sudo_prefix() + ["rm", str(enabled_link)]
            run_elevated_command(cmd_rm_link)

        if service_running:
            self.reload()

    def validate_config(self) -> bool:
        """Runs apache2ctl configtest or httpd -t to check syntax."""
        try:
            if self.os_family == "debian_family":
                cmd = get_sudo_prefix() + ["apache2ctl", "configtest"]
            else:
                cmd = get_sudo_prefix() + ["httpd", "-t"]
            run_elevated_command(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )
            return True
        except (RuntimeError, FileNotFoundError, OSError):
            return False

    def reload(self):
        """Reloads the Apache service."""
        try:
            cmd = get_sudo_prefix() + ["systemctl", "reload", APACHE_SERVICE_NAME]
            run_elevated_command(cmd)
        except RuntimeError as e:
            raise RuntimeError(f"Failed to reload Apache: {e}")
