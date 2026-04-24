"""
QA additions for ULTIMATE_VHOST-015 — covering gaps found during QA phase.

Targets:
- detect_os_family() additional OS IDs (kali, ol, linuxmint, pop, elementary)
- main.py enable/disable command full success and error paths (uncovered lines)
- NginxProvider.enable_vhost() Debian: FileNotFoundError and already-enabled guard
- NginxProvider.disable_vhost() Debian: already-disabled guard
- detect_os_family() with whitespace-only ID value
- RHEL enable/disable via main.py CLI
"""

import subprocess

import pytest
from typer.testing import CliRunner

from vhost_helper.os_detector import detect_os_family
from vhost_helper.providers.nginx import NginxProvider

runner = CliRunner()


# ---------------------------------------------------------------------------
# detect_os_family() — additional OS IDs from _DEBIAN_IDS and _RHEL_IDS sets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("os_id", ["kali", "linuxmint", "pop", "elementary"])
def test_detect_os_family_additional_debian_ids(tmp_path, os_id):
    """Debian-family IDs beyond ubuntu/debian must be classified as debian_family."""
    f = tmp_path / "os-release"
    f.write_text(f'ID={os_id}\nVERSION_ID="1"\n')
    assert (
        detect_os_family(str(f)) == "debian_family"
    ), f"Expected debian_family for ID={os_id}"


@pytest.mark.parametrize("os_id", ["ol", "amzn"])
def test_detect_os_family_additional_rhel_ids(tmp_path, os_id):
    """RHEL-family IDs beyond rhel/centos/fedora must be classified as rhel_family."""
    f = tmp_path / "os-release"
    f.write_text(f'ID={os_id}\nVERSION_ID="1"\n')
    assert (
        detect_os_family(str(f)) == "rhel_family"
    ), f"Expected rhel_family for ID={os_id}"


def test_detect_os_family_whitespace_only_id(tmp_path):
    """ID field containing only whitespace must not match any family (returns unknown)."""
    f = tmp_path / "os-release"
    f.write_text('ID=   \nVERSION_ID="1"\n')
    # Whitespace ID lowercases to empty string after strip — no match
    assert detect_os_family(str(f)) == "unknown"


def test_detect_os_family_id_like_case_insensitive(tmp_path):
    """ID_LIKE values must be compared case-insensitively."""
    f = tmp_path / "os-release"
    f.write_text('ID=somenewdistro\nID_LIKE="Debian"\n')
    assert detect_os_family(str(f)) == "debian_family"


def test_detect_os_family_id_like_rhel_case_insensitive(tmp_path):
    """ID_LIKE=RHEL (upper-case) must still match rhel_family."""
    f = tmp_path / "os-release"
    f.write_text('ID=oracle\nID_LIKE="RHEL"\n')
    assert detect_os_family(str(f)) == "rhel_family"


# ---------------------------------------------------------------------------
# NginxProvider.enable_vhost() Debian — uncovered guard paths
# ---------------------------------------------------------------------------


@pytest.fixture
def debian_nginx_provider(mocker, tmp_path):
    """NginxProvider patched for Debian with tmp dirs and mocked run."""
    sites_available = tmp_path / "sites-available"
    sites_enabled = tmp_path / "sites-enabled"
    sites_available.mkdir()
    sites_enabled.mkdir()

    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_available)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "debian_family")
    mock_run = mocker.patch(
        "vhost_helper.providers.nginx.run_elevated_command",
        return_value=subprocess.CompletedProcess([], 0),
    )

    provider = NginxProvider()
    mocker.patch.object(provider, "reload")

    provider._sites_available = sites_available
    provider._sites_enabled = sites_enabled
    provider._mock_run = mock_run
    return provider


def test_enable_vhost_debian_raises_file_not_found_when_config_absent(
    debian_nginx_provider,
):
    """enable_vhost Debian must raise FileNotFoundError if config file is absent."""
    with pytest.raises(FileNotFoundError, match="not found"):
        debian_nginx_provider.enable_vhost("nonexistent.test")


def test_enable_vhost_debian_noop_when_link_already_exists(debian_nginx_provider):
    """enable_vhost Debian must return silently when the symlink already exists."""
    # Create both config and existing symlink (both require .conf extension)
    (debian_nginx_provider._sites_available / "already.test.conf").touch()
    (debian_nginx_provider._sites_enabled / "already.test.conf").touch()

    debian_nginx_provider.enable_vhost("already.test")

    # No ln command should be issued
    debian_nginx_provider._mock_run.assert_not_called()
    debian_nginx_provider.reload.assert_not_called()


def test_disable_vhost_debian_noop_when_link_absent(debian_nginx_provider):
    """disable_vhost Debian must return silently when the symlink is already absent."""
    debian_nginx_provider.disable_vhost("not-enabled.test")

    debian_nginx_provider._mock_run.assert_not_called()
    debian_nginx_provider.reload.assert_not_called()


# ---------------------------------------------------------------------------
# main.py enable command — full success path (lines 243-266)
# ---------------------------------------------------------------------------


def test_cli_enable_success_with_service_not_running(mocker, tmp_path):
    """enable command must succeed, adding hostfile entries and enabling Nginx config."""
    from vhost_helper.main import app

    available_dir = tmp_path / "sites-available"
    available_dir.mkdir()
    enabled_dir = tmp_path / "sites-enabled"
    enabled_dir.mkdir()
    # To be detected as Nginx, the file should exist in sites-available
    (available_dir / "example.test.conf").touch()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available_dir)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled_dir)
    mocker.patch(
        "vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-available"
    )
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled")
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mock_add = mocker.patch("vhost_helper.main.add_entry")
    mock_provider_enable = mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.enable_vhost"
    )

    result = runner.invoke(app, ["enable", "example.test"])

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert mock_add.call_count == 2
    mock_provider_enable.assert_called_once_with("example.test", service_running=False)
    assert "enabled" in result.output.lower()


def test_cli_enable_success_with_service_running(mocker, tmp_path):
    """enable command with running Nginx service must report reload success."""
    from vhost_helper.main import app

    available_dir = tmp_path / "sites-available"
    available_dir.mkdir()
    enabled_dir = tmp_path / "sites-enabled"
    enabled_dir.mkdir()
    (available_dir / "running.test.conf").touch()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available_dir)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled_dir)
    mocker.patch(
        "vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-available"
    )
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled")
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.enable_vhost")

    result = runner.invoke(app, ["enable", "running.test"])

    assert result.exit_code == 0
    assert "reloaded" in result.output.lower()


def test_cli_enable_error_path_exits_1(mocker, tmp_path):
    """enable command must exit with code 1 and print error when provider raises."""
    from vhost_helper.main import app

    available_dir = tmp_path / "sites-available"
    available_dir.mkdir()
    enabled_dir = tmp_path / "sites-enabled"
    enabled_dir.mkdir()
    (available_dir / "error.test.conf").touch()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available_dir)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled_dir)
    mocker.patch(
        "vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-available"
    )
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled")
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.enable_vhost",
        side_effect=RuntimeError("nginx enable failed"),
    )

    result = runner.invoke(app, ["enable", "error.test"])

    assert result.exit_code == 1
    assert "nginx enable failed" in result.output


def test_cli_enable_invalid_domain_exits_1():
    """enable command must validate domain and exit 1 on invalid input."""
    from vhost_helper.main import app

    result = runner.invoke(app, ["enable", "bad..domain"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# main.py disable command — full success path (lines 287-310)
# ---------------------------------------------------------------------------


def test_cli_disable_success_with_service_not_running(mocker, tmp_path):
    """disable command must remove hostfile entries and disable Nginx config."""
    from vhost_helper.main import app

    sites_enabled = tmp_path / "sites-enabled"
    sites_enabled.mkdir()
    # Domain IS currently enabled (file exists in sites-enabled with .conf extension)
    (sites_enabled / "enabled.test.conf").touch()

    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mock_remove = mocker.patch("vhost_helper.main.remove_entry")
    mock_provider_disable = mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.disable_vhost"
    )

    result = runner.invoke(app, ["disable", "enabled.test"])

    assert result.exit_code == 0, f"Unexpected exit: {result.output}"
    assert mock_remove.call_count == 2
    mock_provider_disable.assert_called_once_with("enabled.test", service_running=False)
    assert "disabled" in result.output.lower()


def test_cli_disable_success_with_service_running(mocker, tmp_path):
    """disable command with running Nginx service must report reload success."""
    from vhost_helper.main import app

    sites_enabled = tmp_path / "sites-enabled"
    sites_enabled.mkdir()
    (sites_enabled / "live.test.conf").touch()

    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.disable_vhost")

    result = runner.invoke(app, ["disable", "live.test"])

    assert result.exit_code == 0
    assert "reloaded" in result.output.lower()


def test_cli_disable_error_path_exits_1(mocker, tmp_path):
    """disable command must exit with code 1 and print error when provider raises."""
    from vhost_helper.main import app

    sites_enabled = tmp_path / "sites-enabled"
    sites_enabled.mkdir()
    (sites_enabled / "fail.test.conf").touch()

    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.disable_vhost",
        side_effect=RuntimeError("nginx disable failed"),
    )

    result = runner.invoke(app, ["disable", "fail.test"])

    assert result.exit_code == 1
    assert "nginx disable failed" in result.output


def test_cli_disable_invalid_domain_exits_1():
    """disable command must validate domain and exit 1 on invalid input."""
    from vhost_helper.main import app

    result = runner.invoke(app, ["disable", "bad..domain"])
    assert result.exit_code == 1
