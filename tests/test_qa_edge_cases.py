import pytest
from pathlib import Path
from pydantic import ValidationError
from vhost_helper.models import VHostConfig
from vhost_helper.main import app
from typer.testing import CliRunner
import subprocess
import re
import os
from unittest.mock import MagicMock

runner = CliRunner()


@pytest.fixture(autouse=True)
def mock_privileged_ops(mocker, tmp_path):
    # Mocking at the source
    mocker.patch("vhost_helper.utils.get_sudo_prefix", return_value=[])
    mocker.patch(
        "vhost_helper.utils.run_elevated_command",
        return_value=subprocess.CompletedProcess([], 0),
    )
    mocker.patch("vhost_helper.utils.preflight_sudo_check", return_value=None)

    # Mocking where they are imported
    mocker.patch("vhost_helper.main.preflight_sudo_check", return_value=None)
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)
    mocker.patch("vhost_helper.main.add_entry", return_value=True)
    mocker.patch("vhost_helper.main.remove_entry", return_value=True)

    # Mock Nginx and Apache paths to temp dirs
    nginx_avail = tmp_path / "nginx-available"
    nginx_avail.mkdir(exist_ok=True)
    nginx_enabled = tmp_path / "nginx-enabled"
    nginx_enabled.mkdir(exist_ok=True)
    apache_avail = tmp_path / "apache-available"
    apache_avail.mkdir(exist_ok=True)
    apache_enabled = tmp_path / "apache-enabled"
    apache_enabled.mkdir(exist_ok=True)

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", nginx_avail)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", nginx_enabled)
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", apache_avail)
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", apache_enabled)

    # Do NOT mock NginxProvider methods globally if we want to test them.
    mocker.patch(
        "vhost_helper.providers.nginx.run_elevated_command",
        return_value=subprocess.CompletedProcess([], 0),
    )


def test_vhost_config_domain_redos_potential(tmp_path):
    long_label = "a" * 63
    long_domain = f"{long_label}.{long_label}.{long_label}.test"
    config = VHostConfig(domain=long_domain, document_root=tmp_path)
    assert config.domain == long_domain


def test_vhost_config_document_root_not_exists():
    with pytest.raises(ValidationError) as excinfo:
        VHostConfig(
            domain="example.test", document_root=Path("/non/existent/path/at/all")
        )
    assert "does not exist" in str(excinfo.value)


def test_vhost_config_invalid_document_root_injection(mocker):
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.is_dir", return_value=True)
    with pytest.raises(ValidationError) as excinfo:
        VHostConfig(domain="example.test", document_root=Path('/tmp/dir"withquote'))
    assert "forbidden characters" in str(excinfo.value)


def test_vhost_config_document_root_not_dir(tmp_path):
    file_path = tmp_path / "not_a_dir"
    file_path.touch()
    with pytest.raises(ValidationError) as excinfo:
        VHostConfig(domain="example.test", document_root=file_path)
    assert "must be a directory" in str(excinfo.value)


def test_cli_create_with_invalid_document_root(tmp_path):
    file_path = tmp_path / "index.html"
    file_path.touch()
    result = runner.invoke(app, ["create", "example.test", str(file_path)])
    assert result.exit_code != 0
    output = re.sub(r"\s+", " ", result.output)
    # The scaffolding layer intercepts the file-not-a-directory case before
    # Pydantic validation; the CLI message is "exists but is not a directory".
    assert "not a directory" in output


def test_cli_info_permission_denied(mocker, tmp_path):
    nginx_avail = tmp_path / "nginx-available"
    nginx_avail.mkdir(exist_ok=True)
    conf_file = nginx_avail / "example.test.conf"
    conf_file.touch()

    # We need to mock Path.exists and Path.read_text specifically for our test file
    # but let other paths work normally or they might break internal logic.
    original_exists = Path.exists

    def patched_exists(self):
        if str(self).endswith("example.test.conf"):
            return True
        return original_exists(self)

    mocker.patch.object(Path, "exists", patched_exists)
    mocker.patch.object(
        Path, "read_text", side_effect=PermissionError("Permission denied")
    )

    result = runner.invoke(app, ["info", "example.test"])
    assert "Permission denied reading configuration" in result.output


def test_cli_info_generic_error(mocker, tmp_path):
    nginx_avail = tmp_path / "nginx-available"
    nginx_avail.mkdir(exist_ok=True)
    conf_file = nginx_avail / "example.test.conf"
    conf_file.touch()

    original_exists = Path.exists

    def patched_exists(self):
        if str(self).endswith("example.test.conf"):
            return True
        return original_exists(self)

    mocker.patch.object(Path, "exists", patched_exists)
    mocker.patch.object(Path, "read_text", side_effect=Exception("Generic Error"))

    result = runner.invoke(app, ["info", "example.test"])
    assert "Error reading configuration" in result.output
    assert "Generic Error" in result.output


def test_cli_info_os_detection_error(mocker):
    mocker.patch(
        "vhost_helper.main.get_os_info", side_effect=Exception("OS detection failure")
    )
    result = runner.invoke(app, ["info"])
    assert "Error detecting OS: OS detection failure" in result.output


def test_cli_create_nginx_notification_when_stopped(mocker, tmp_path):
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.create_vhost", return_value=None
    )
    result = runner.invoke(app, ["create", "example.test", str(tmp_path)])
    assert result.exit_code == 0
    assert "Nginx is installed but not running" in result.output


def test_cli_remove_nginx_notification_when_stopped(mocker, tmp_path):
    nginx_avail = tmp_path / "nginx-available"
    nginx_avail.mkdir(exist_ok=True)
    (nginx_avail / "example.test.conf").write_text("# Generated by VHost Helper\n")

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", nginx_avail)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nginx-enabled")
    mocker.patch(
        "vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-available"
    )
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled")
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("typer.confirm", return_value=True)
    mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.remove_vhost", return_value=None
    )
    result = runner.invoke(app, ["remove", "example.test"])
    assert result.exit_code == 0
    assert "Nginx is installed but not running" in result.output


def test_cli_remove_generic_error(mocker, tmp_path):
    nginx_avail = tmp_path / "nginx-available"
    nginx_avail.mkdir(exist_ok=True)
    (nginx_avail / "example.test.conf").write_text("# Generated by VHost Helper\n")

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", nginx_avail)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nginx-enabled")
    mocker.patch(
        "vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-available"
    )
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled")
    mocker.patch("typer.confirm", return_value=True)
    mocker.patch(
        "vhost_helper.main.remove_entry", side_effect=Exception("Remove Error")
    )
    result = runner.invoke(app, ["remove", "example.test"])
    assert result.exit_code != 0
    assert "Error: Remove Error" in result.output


def test_cli_list_permission_denied(mocker):
    mock_dir = MagicMock(spec=Path)
    mock_dir.exists.return_value = True
    mock_file = MagicMock(spec=Path)
    # mock_file.name, suffix, and stem must return strings matching a .conf file
    type(mock_file).name = mocker.PropertyMock(return_value="example.test.conf")
    type(mock_file).suffix = mocker.PropertyMock(return_value=".conf")
    type(mock_file).stem = mocker.PropertyMock(return_value="example.test")
    mock_file.exists.return_value = True
    mock_file.read_text.side_effect = PermissionError("Permission denied")
    mock_dir.iterdir.return_value = [mock_file]
    mock_dir.__truediv__.return_value = mock_file
    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", mock_dir)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", mock_dir)
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "Permission denied reading configuration" in result.output


def test_os_detector_unexpected_error(mocker):
    mocker.patch("subprocess.run", side_effect=Exception("Unforeseen system error"))
    from vhost_helper.os_detector import get_os_info

    with pytest.raises(RuntimeError, match="Unexpected error"):
        get_os_info()


def test_nginx_reload_failure_both_methods(mocker):
    from vhost_helper.providers.nginx import NginxProvider

    mocker.patch(
        "vhost_helper.providers.nginx.run_elevated_command",
        side_effect=RuntimeError("Both failed"),
    )
    mocker.patch("pathlib.Path.exists", return_value=True)
    mocker.patch("pathlib.Path.is_dir", return_value=True)
    provider = NginxProvider()
    with pytest.raises(RuntimeError, match="Failed to reload Nginx"):
        provider.reload()


def test_nginx_remove_vhost_failure(mocker):
    from vhost_helper.providers.nginx import NginxProvider

    mocker.patch(
        "vhost_helper.providers.nginx.run_elevated_command",
        side_effect=RuntimeError("Remove failed"),
    )
    mocker.patch("pathlib.Path.exists", return_value=True)
    provider = NginxProvider()
    with pytest.raises(RuntimeError, match="Failed to remove Nginx vhost"):
        provider.remove_vhost("example.test")


def test_hostfile_rollback_failure_logs_error(mocker, tmp_path):
    mocker.patch(
        "vhost_helper.main.NginxProvider.create_vhost",
        side_effect=Exception("Nginx Error"),
    )
    mocker.patch(
        "vhost_helper.main.remove_entry", side_effect=Exception("Rollback Error")
    )
    result = runner.invoke(app, ["create", "example.test", str(tmp_path)])
    assert result.exit_code != 0
    assert "Error during hostfile rollback: Rollback Error" in result.output


def test_config_env_fallback(mocker):
    mocker.patch.dict(
        os.environ, {"VHOST_TEST_MODE": "1", "VHOST_HOSTS_FILE": "/tmp/custom_hosts"}
    )
    from vhost_helper.config import _get_path

    path = _get_path("VHOST_HOSTS_FILE", "/etc/hosts")
    assert str(path) == "/tmp/custom_hosts"
