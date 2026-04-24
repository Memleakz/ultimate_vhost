import subprocess
import shutil
import tempfile
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, TemplateNotFound
from ..models import VHostConfig
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

    def enable_vhost(self, domain: str, service_running: bool = True):
        """
        Enables a virtual host.
        - Debian: Creates a symbolic link.
        - RHEL: Moves the file from 'conf.disabled' to 'conf.d'.
        """
        if self.os_family == "rhel_family":
            if not APACHE_SITES_DISABLED:
                raise RuntimeError("APACHE_SITES_DISABLED path is not configured.")
            disabled_config_path = APACHE_SITES_DISABLED / (domain + ".conf")
            enabled_config_path = APACHE_SITES_ENABLED / (domain + ".conf")

            if not disabled_config_path.exists():
                raise FileNotFoundError(
                    f"Configuration for {domain} not found in {APACHE_SITES_DISABLED}."
                )
            if enabled_config_path.exists():
                return  # Already enabled

            cmd_mv = get_sudo_prefix() + [
                "mv",
                str(disabled_config_path),
                str(enabled_config_path),
            ]
            run_elevated_command(cmd_mv)

        else:  # Debian family
            config_path = APACHE_SITES_AVAILABLE / (domain + ".conf")
            enabled_link = APACHE_SITES_ENABLED / (domain + ".conf")
            if not config_path.exists():
                raise FileNotFoundError(f"Configuration for {domain} not found.")
            if enabled_link.exists():
                return  # Already enabled

            cmd_ln = get_sudo_prefix() + [
                "ln",
                "-s",
                str(config_path),
                str(enabled_link),
            ]
            run_elevated_command(cmd_ln)

        if service_running:
            self.reload()

    def disable_vhost(self, domain: str, service_running: bool = True):
        """
        Disables a virtual host.
        - Debian: Removes the symbolic link.
        - RHEL: Moves the file from 'conf.d' to 'conf.disabled'.
        """
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

            enabled_config_path = APACHE_SITES_ENABLED / (domain + ".conf")
            disabled_config_path = APACHE_SITES_DISABLED / (domain + ".conf")
            if not enabled_config_path.exists():
                return  # Already disabled

            cmd_mv = get_sudo_prefix() + [
                "mv",
                str(enabled_config_path),
                str(disabled_config_path),
            ]
            run_elevated_command(cmd_mv)

        else:  # Debian family
            enabled_link = APACHE_SITES_ENABLED / (domain + ".conf")
            if not enabled_link.exists() and not enabled_link.is_symlink():
                return  # Already disabled

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
