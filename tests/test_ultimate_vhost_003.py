"""
Tests for ULTIMATE_VHOST-003: Support VHost Generation for Inactive Nginx Service.

Acceptance criteria verified here:
1. VHost files are written to disk when nginx is installed but the service is stopped.
2. The exact warning notification is printed when the service is inactive.
3. The tool hard-fails (exit 1) when the nginx binary is missing from PATH.
4. No service-management commands (systemctl start / service nginx start) are invoked.
"""

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vhost_helper.main import app
from vhost_helper.models import VHostConfig, ServerType
from vhost_helper.providers.nginx import (
    NginxProvider,
    is_nginx_installed,
    is_nginx_running,
)

runner = CliRunner()

INACTIVE_WARNING = (
    "Notification: Nginx is installed but not running. "
    "You must start the service manually to apply these changes."
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_nginx_dirs(mocker):
    """Patch nginx config directories to point at a temporary tree."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        available = tmp_path / "sites-available"
        enabled = tmp_path / "sites-enabled"
        available.mkdir()
        enabled.mkdir()
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)
        mocker.patch("vhost_helper.providers.nginx.detected_os_family", "debian_family")
        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
        yield available, enabled, tmp_path


@pytest.fixture
def doc_root(tmp_path):
    """Return a temporary directory that acts as the document root."""
    root = tmp_path / "project"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# Unit tests: is_nginx_installed / is_nginx_running
# ---------------------------------------------------------------------------


def test_is_nginx_installed_returns_true_when_binary_on_path(mocker):
    mocker.patch("shutil.which", return_value="/usr/sbin/nginx")
    assert is_nginx_installed() is True


def test_is_nginx_installed_returns_false_when_binary_missing(mocker):
    mocker.patch("shutil.which", return_value=None)
    assert is_nginx_installed() is False


def test_is_nginx_running_returns_true_when_active(mocker):
    mock_result = mocker.MagicMock()
    mock_result.stdout = "active\n"
    mocker.patch("subprocess.run", return_value=mock_result)
    assert is_nginx_running() is True


def test_is_nginx_running_returns_false_when_inactive(mocker):
    mock_result = mocker.MagicMock()
    mock_result.stdout = "inactive\n"
    mocker.patch("subprocess.run", return_value=mock_result)
    assert is_nginx_running() is False


def test_is_nginx_running_returns_false_when_systemctl_missing(mocker):
    mocker.patch("subprocess.run", side_effect=FileNotFoundError)
    assert is_nginx_running() is False


# ---------------------------------------------------------------------------
# Unit tests: NginxProvider.create_vhost with service_running=False
# ---------------------------------------------------------------------------


def test_create_vhost_writes_files_when_service_stopped(tmp_nginx_dirs, mocker):
    """Config file and symlink are created even when nginx service is stopped."""
    import subprocess

    available, enabled, tmp_path = tmp_nginx_dirs

    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")

    provider = NginxProvider()
    config = VHostConfig(
        domain="stopped.test",
        document_root=tmp_path,
        port=80,
        server_type=ServerType.NGINX,
    )

    provider.create_vhost(config, service_running=False)

    calls = [c[0][0] for c in subprocess.run.call_args_list]
    assert any(
        "mv" in c for c in calls
    ), "Config file should be moved to sites-available"
    assert any("ln" in c for c in calls), "Symlink should be created in sites-enabled"


def test_create_vhost_does_not_call_validate_or_reload_when_stopped(
    tmp_nginx_dirs, mocker
):
    """validate_config and reload must NOT be called when service is stopped."""
    import subprocess

    available, enabled, tmp_path = tmp_nginx_dirs

    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")
    mock_validate = mocker.patch.object(
        NginxProvider, "validate_config", return_value=True
    )
    mock_reload = mocker.patch.object(NginxProvider, "reload")

    provider = NginxProvider()
    config = VHostConfig(
        domain="stopped.test",
        document_root=tmp_path,
        port=80,
        server_type=ServerType.NGINX,
    )

    provider.create_vhost(config, service_running=False)

    mock_validate.assert_not_called()
    mock_reload.assert_not_called()


def test_create_vhost_calls_validate_and_reload_when_running(tmp_nginx_dirs, mocker):
    """Existing behaviour: validate_config and reload ARE called when service is running."""
    import subprocess

    available, enabled, tmp_path = tmp_nginx_dirs

    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")
    mock_validate = mocker.patch.object(
        NginxProvider, "validate_config", return_value=True
    )
    mock_reload = mocker.patch.object(NginxProvider, "reload")

    provider = NginxProvider()
    config = VHostConfig(
        domain="running.test",
        document_root=tmp_path,
        port=80,
        server_type=ServerType.NGINX,
    )

    provider.create_vhost(config, service_running=True)

    mock_validate.assert_called_once()
    mock_reload.assert_called_once()


# ---------------------------------------------------------------------------
# Integration tests: CLI `vhost create` command
# ---------------------------------------------------------------------------


def test_cli_create_succeeds_with_stopped_service(tmp_nginx_dirs, doc_root, mocker):
    """Exit code is 0 and warning is shown when nginx is installed but stopped."""
    available, enabled, _ = tmp_nginx_dirs

    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")

    result = runner.invoke(app, ["create", "stopped.test", str(doc_root)])

    assert result.exit_code == 0, result.output
    # Rich may wrap the long warning string; check key fragments instead.
    assert "Notification: Nginx is installed but not running" in result.output
    assert "manually to apply these changes" in result.output


def test_cli_create_shows_skipped_steps_when_service_stopped(
    tmp_nginx_dirs, doc_root, mocker
):
    """Skipped-step indicators must appear in output when service is stopped."""
    available, enabled, _ = tmp_nginx_dirs

    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")

    result = runner.invoke(app, ["create", "stopped.test", str(doc_root)])

    assert "⊘ Skipped" in result.output
    assert "Nginx configuration validation" in result.output
    assert "Nginx reload" in result.output


def test_cli_create_fails_hard_when_nginx_not_installed(
    tmp_nginx_dirs, doc_root, mocker
):
    """Exit code is 1 and 'No supported web server found' error is shown when binary is missing."""
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=False)
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

    result = runner.invoke(app, ["create", "missing.test", str(doc_root)])

    assert result.exit_code == 1
    assert "No supported web server found" in result.output


def test_cli_create_does_not_invoke_service_start(tmp_nginx_dirs, doc_root, mocker):
    """The tool must never call systemctl start or service nginx start."""
    available, enabled, _ = tmp_nginx_dirs

    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")

    mock_run = mocker.patch("subprocess.run")

    runner.invoke(app, ["create", "stopped.test", str(doc_root)])

    for call in mock_run.call_args_list:
        cmd = call[0][0] if call[0] else call[1].get("args", [])
        cmd_str = " ".join(str(c) for c in cmd)
        assert "systemctl start" not in cmd_str, f"Forbidden command invoked: {cmd_str}"
        assert (
            "service nginx start" not in cmd_str
        ), f"Forbidden command invoked: {cmd_str}"


def test_cli_create_succeeds_with_running_service(tmp_nginx_dirs, doc_root, mocker):
    """Existing happy path still works: exit 0, site URL shown, no warning."""
    available, enabled, _ = tmp_nginx_dirs

    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")

    result = runner.invoke(app, ["create", "running.test", str(doc_root)])

    assert result.exit_code == 0, result.output
    assert "http://running.test" in result.output
    assert INACTIVE_WARNING not in result.output
