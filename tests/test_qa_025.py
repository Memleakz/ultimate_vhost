import pytest
from typer.testing import CliRunner

from vhost_helper.main import app

runner = CliRunner()

# Define a baseline non-managed config content
APACHE_EXTERNAL_CONF = """
<VirtualHost *:80>
    ServerName external-apache.test
    DocumentRoot /var/www/external-apache
</VirtualHost>
"""

NGINX_EXTERNAL_CONF = """
server {
    listen 80;
    server_name external-nginx.test;
    root /var/www/external-nginx;
    index index.html;
}
"""


@pytest.fixture(scope="function")
def mixed_env(tmp_path, monkeypatch):
    """
    Sets up a temporary environment with a mix of managed and external vhosts
    for both Apache and Nginx.
    """
    # Create mock directories for both providers
    # These paths need to be patched in the config module
    apache_avail = tmp_path / "apache2" / "sites-available"
    apache_enabled = tmp_path / "apache2" / "sites-enabled"
    nginx_avail = tmp_path / "nginx" / "sites-available"
    nginx_enabled = tmp_path / "nginx" / "sites-enabled"

    apache_avail.mkdir(parents=True, exist_ok=True)
    apache_enabled.mkdir(parents=True, exist_ok=True)
    nginx_avail.mkdir(parents=True, exist_ok=True)
    nginx_enabled.mkdir(parents=True, exist_ok=True)

    web_root = tmp_path / "web"
    web_root.mkdir()

    # --- Create External VHosts (initially disabled) ---
    (apache_avail / "external-apache.test.conf").write_text(APACHE_EXTERNAL_CONF)
    (nginx_avail / "external-nginx.test.conf").write_text(NGINX_EXTERNAL_CONF)

    # Use monkeypatch to override the config paths
    monkeypatch.setattr("vhost_helper.config.APACHE_SITES_AVAILABLE", apache_avail)
    monkeypatch.setattr("vhost_helper.config.APACHE_SITES_ENABLED", apache_enabled)
    monkeypatch.setattr("vhost_helper.config.NGINX_SITES_AVAILABLE", nginx_avail)
    monkeypatch.setattr("vhost_helper.config.NGINX_SITES_ENABLED", nginx_enabled)

    # Patch imported paths in other modules
    monkeypatch.setattr("vhost_helper.main.APACHE_SITES_AVAILABLE", apache_avail)
    monkeypatch.setattr("vhost_helper.main.APACHE_SITES_ENABLED", apache_enabled)
    monkeypatch.setattr("vhost_helper.main.NGINX_SITES_AVAILABLE", nginx_avail)
    monkeypatch.setattr("vhost_helper.main.NGINX_SITES_ENABLED", nginx_enabled)

    monkeypatch.setattr(
        "vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", nginx_avail
    )
    monkeypatch.setattr(
        "vhost_helper.providers.nginx.NGINX_SITES_ENABLED", nginx_enabled
    )

    monkeypatch.setattr(
        "vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", apache_avail
    )
    monkeypatch.setattr(
        "vhost_helper.providers.apache.APACHE_SITES_ENABLED", apache_enabled
    )

    # Patch run_elevated_command to strip 'sudo' and run as current user
    def mock_run_elevated(cmd, **kwargs):
        if cmd and cmd[0] == "sudo":
            cmd = cmd[1:]
        import subprocess

        # If we are running tests, we don't care about checking exit codes strictly
        # But we let subprocess handle it.
        return subprocess.run(cmd, **kwargs)

    monkeypatch.setattr("vhost_helper.utils.run_elevated_command", mock_run_elevated)
    monkeypatch.setattr(
        "vhost_helper.scaffolding.run_elevated_command", mock_run_elevated
    )
    monkeypatch.setattr(
        "vhost_helper.permissions.run_elevated_command", mock_run_elevated
    )
    monkeypatch.setattr(
        "vhost_helper.providers.nginx.run_elevated_command", mock_run_elevated
    )
    monkeypatch.setattr(
        "vhost_helper.providers.apache.run_elevated_command", mock_run_elevated
    )

    # Mock os_family to be debian for symlinking
    monkeypatch.setattr(
        "vhost_helper.providers.apache.detected_os_family", "debian_family"
    )
    monkeypatch.setattr(
        "vhost_helper.providers.nginx.detected_os_family", "debian_family"
    )

    # Mock service checks
    monkeypatch.setattr("vhost_helper.main.is_apache_installed", lambda: True)
    monkeypatch.setattr("vhost_helper.main.is_nginx_installed", lambda: True)
    monkeypatch.setattr("vhost_helper.main.is_apache_running", lambda: False)
    monkeypatch.setattr("vhost_helper.main.is_nginx_running", lambda: False)
    monkeypatch.setattr("vhost_helper.main.preflight_sudo_check", lambda: None)
    monkeypatch.setenv("COLUMNS", "250")

    # Mock os_family to be debian for symlinking
    monkeypatch.setattr(
        "vhost_helper.providers.apache.detected_os_family", "debian_family"
    )
    monkeypatch.setattr(
        "vhost_helper.providers.nginx.detected_os_family", "debian_family"
    )

    # Mock service checks
    monkeypatch.setattr("vhost_helper.main.is_apache_installed", lambda: True)
    monkeypatch.setattr("vhost_helper.main.is_nginx_installed", lambda: True)
    monkeypatch.setattr("vhost_helper.main.is_apache_running", lambda: False)
    monkeypatch.setattr("vhost_helper.main.is_nginx_running", lambda: False)
    monkeypatch.setattr("vhost_helper.main.preflight_sudo_check", lambda: None)

    # --- Create Managed VHosts using the CLI ---
    # Managed Apache (enabled)
    runner.invoke(
        app,
        [
            "create",
            "managed-apache.test",
            str(web_root),
            "--provider",
            "apache",
            "--no-scaffold",
        ],
        catch_exceptions=False,
    )
    # Managed Nginx (disabled) - create then disable
    runner.invoke(
        app,
        [
            "create",
            "managed-nginx.test",
            str(web_root),
            "--provider",
            "nginx",
            "--no-scaffold",
        ],
        catch_exceptions=False,
    )
    runner.invoke(
        app,
        ["disable", "managed-nginx.test", "--provider", "nginx"],
        catch_exceptions=False,
    )

    yield {
        "apache_avail": apache_avail,
        "apache_enabled": apache_enabled,
        "nginx_avail": nginx_avail,
        "nginx_enabled": nginx_enabled,
    }


def test_vhost_list_mixed_environment(mixed_env):
    """
    Test `vhost list` shows all vhosts with correct origin and status.
    """
    result = runner.invoke(app, ["list"], catch_exceptions=False)
    assert result.exit_code == 0
    output = result.stdout
    print(f"\n--- VHOST LIST OUTPUT ---\n{output}\n-------------------------")

    # Check for all 4 domains
    assert "managed-apache.test" in output
    assert "managed-nginx.test" in output
    assert "external-apache.test" in output
    assert "external-nginx.test" in output

    # Check origins
    assert "managed-apache.test" in output and "VHost Helper" in output
    assert "managed-nginx.test" in output and "VHost Helper" in output
    assert "external-apache.test" in output and "External" in output
    assert "external-nginx.test" in output and "External" in output

    # Check statuses
    assert "managed-apache.test" in output and "Enabled" in output
    assert "managed-nginx.test" in output and "Disabled" in output
    assert "external-apache.test" in output and "Disabled" in output
    assert "external-nginx.test" in output and "Disabled" in output


def test_enable_disable_external_vhost(mixed_env):
    """
    Test that `enable` and `disable` work on external vhosts without modifying them.
    """
    apache_conf_path = mixed_env["apache_avail"] / "external-apache.test.conf"
    apache_enabled_link = mixed_env["apache_enabled"] / "external-apache.test.conf"

    # 1. Enable external Apache vhost
    result_enable = runner.invoke(
        app,
        ["enable", "external-apache.test", "--provider", "apache"],
        catch_exceptions=False,
    )
    assert result_enable.exit_code == 0
    assert (
        "Virtual host 'external-apache.test' has been enabled." in result_enable.stdout
    )
    assert apache_enabled_link.is_symlink()
    assert apache_enabled_link.resolve() == apache_conf_path

    # Verify content is unchanged
    assert apache_conf_path.read_text() == APACHE_EXTERNAL_CONF

    # 2. Check list output
    result_list = runner.invoke(app, ["list"], catch_exceptions=False)
    assert (
        "external-apache.test" in result_list.stdout and "Enabled" in result_list.stdout
    )

    # 3. Disable external Apache vhost
    result_disable = runner.invoke(
        app,
        ["disable", "external-apache.test", "--provider", "apache"],
        catch_exceptions=False,
    )
    assert result_disable.exit_code == 0
    assert (
        "Virtual host 'external-apache.test' has been disabled."
        in result_disable.stdout
    )
    assert not apache_enabled_link.exists()

    # Verify content is still unchanged
    assert apache_conf_path.read_text() == APACHE_EXTERNAL_CONF


def test_remove_external_vhost_is_allowed(mixed_env):
    """
    Test that `vhost remove` is allowed for external vhosts.
    """
    result = runner.invoke(
        app, ["remove", "external-nginx.test", "--force"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "Success!" in result.stdout

    # Verify the file no longer exists
    assert not (mixed_env["nginx_avail"] / "external-nginx.test.conf").exists()


def test_create_external_vhost_is_blocked(mixed_env):
    """
    Test that `vhost create` is blocked if the domain is used by an external vhost.
    """
    result = runner.invoke(
        app,
        [
            "create",
            "external-apache.test",
            "/var/www/external-apache",
            "--provider",
            "apache",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert "Error: Cannot create managed vhost." in result.stdout


def test_info_external_vhost_is_allowed(mixed_env):
    """
    Test that `vhost info` is allowed for external vhosts.
    """
    result = runner.invoke(
        app, ["info", "external-apache.test"], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "external-apache.test" in result.stdout
    assert "External" in result.stdout
