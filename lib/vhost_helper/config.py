import os
from pathlib import Path
from .os_detector import get_os_info, detect_os_family, OSInfo

# This is the root of the installable application package
# It's resolved relative to this file, so it will work when installed system-wide
APP_ROOT = Path(__file__).resolve().parent.parent.parent


def _get_path(env_var: str, default: str) -> Path:
    """
    Returns a Path object, allowing environment variable overrides ONLY if
    VHOST_TEST_MODE is set. This prevents unprivileged users from
    redirecting critical system paths when the tool is executed with sudo.
    """
    if os.getenv("VHOST_TEST_MODE") == "1":
        return Path(os.getenv(env_var, default))
    return Path(default)


def initialize_user_config():
    """
    Creates the user's custom template directory if it doesn't exist.
    This ensures that users can simply drop in their own templates to
    override the defaults. Errors are silently suppressed so that startup
    never crashes due to permission issues or a file occupying the path.
    """
    try:
        user_nginx_templates_path = USER_TEMPLATES_DIR / "nginx"
        user_nginx_templates_path.mkdir(parents=True, exist_ok=True)
    except (PermissionError, NotADirectoryError, OSError):
        pass


# Attempt to get OS info, but gracefully handle failure
try:
    os_info = get_os_info()
except (RuntimeError, FileNotFoundError):
    os_info = OSInfo(id="unknown", version="unknown", family="unknown")

# Determine the canonical OS family using direct /etc/os-release parsing.
# Returns 'debian_family', 'rhel_family', or 'unknown'.
# Falls back gracefully: 'unknown' maps to Debian-style paths.
detected_os_family: str = detect_os_family()

HOSTS_FILE = _get_path("VHOST_HOSTS_FILE", "/etc/hosts")

# Global user-level configuration directory
USER_CONFIG_DIR = _get_path(
    "VHOST_USER_CONFIG_DIR", str(Path.home() / ".config" / "vhost_helper")
)
USER_TEMPLATES_DIR = USER_CONFIG_DIR / "templates"

# Default bundled templates that ship with the application
APP_TEMPLATES_DIR = APP_ROOT / "templates"


# Set Nginx paths based on the detected OS family
if detected_os_family == "rhel_family":
    NGINX_SITES_AVAILABLE = _get_path("NGINX_SITES_AVAILABLE", "/etc/nginx/conf.d")
    NGINX_SITES_ENABLED = _get_path("NGINX_SITES_ENABLED", "/etc/nginx/conf.d")
    NGINX_SITES_DISABLED = _get_path("NGINX_SITES_DISABLED", "/etc/nginx/conf.disabled")

    APACHE_SITES_AVAILABLE = _get_path("APACHE_SITES_AVAILABLE", "/etc/httpd/conf.d")
    APACHE_SITES_ENABLED = _get_path("APACHE_SITES_ENABLED", "/etc/httpd/conf.d")
    APACHE_SITES_DISABLED = _get_path(
        "APACHE_SITES_DISABLED", "/etc/httpd/conf.disabled"
    )
    APACHE_SERVICE_NAME = "httpd"
else:  # 'debian_family' or 'unknown' — safe default
    NGINX_SITES_AVAILABLE = _get_path(
        "NGINX_SITES_AVAILABLE", "/etc/nginx/sites-available"
    )
    NGINX_SITES_ENABLED = _get_path("NGINX_SITES_ENABLED", "/etc/nginx/sites-enabled")
    NGINX_SITES_DISABLED = None  # Not used in Debian-style management

    APACHE_SITES_AVAILABLE = _get_path(
        "APACHE_SITES_AVAILABLE", "/etc/apache2/sites-available"
    )
    APACHE_SITES_ENABLED = _get_path(
        "APACHE_SITES_ENABLED", "/etc/apache2/sites-enabled"
    )
    APACHE_SITES_DISABLED = None  # Not used in Debian-style management
    APACHE_SERVICE_NAME = "apache2"
