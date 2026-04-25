"""
CLI integration tests for ``vhost logs`` command.

All filesystem access and subprocess calls are fully mocked — no root access
required.
"""

import subprocess
import pytest
from typer.testing import CliRunner
from vhost_helper.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixture: mock both providers' directory layout
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nginx_enabled(mocker, tmp_path):
    """Sets up a mock Nginx enabled directory with a config file."""
    enabled = tmp_path / "nginx-enabled"
    enabled.mkdir()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", enabled)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-avail")
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled")
    mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)

    return enabled


@pytest.fixture
def mock_apache_enabled(mocker, tmp_path):
    """Sets up a mock Apache enabled directory with a config file."""
    enabled = tmp_path / "apache-enabled"
    enabled.mkdir()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", tmp_path / "nginx-avail")
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nginx-enabled")
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", enabled)
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)

    return enabled


# ---------------------------------------------------------------------------
# Helper: write a minimal Nginx config with log directives
# ---------------------------------------------------------------------------


def _write_nginx_config(directory, domain, access_path, error_path):
    conf = directory / f"{domain}.conf"
    conf.write_text(
        f"server {{\n"
        f"    access_log {access_path};\n"
        f"    error_log {error_path};\n"
        f"}}\n"
    )
    return conf


def _write_apache_config(directory, domain, access_path, error_path):
    conf = directory / f"{domain}.conf"
    conf.write_text(
        f"<VirtualHost *:80>\n"
        f"    CustomLog {access_path} combined\n"
        f"    ErrorLog {error_path}\n"
        f"</VirtualHost>\n"
    )
    return conf


# ---------------------------------------------------------------------------
# Happy-path tests — Nginx
# ---------------------------------------------------------------------------


def test_logs_nginx_both_streams(mock_nginx_enabled, tmp_path, mocker):
    """Default invocation tails both access and error logs for an Nginx vhost."""
    domain = "myapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    error_log.touch()

    _write_nginx_config(mock_nginx_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.wait.return_value = 0

    result = runner.invoke(app, ["logs", domain])

    assert result.exit_code == 0
    mock_popen.assert_called_once()
    call_args = mock_popen.call_args[0][0]
    assert call_args[0] == "/usr/bin/tail"
    assert call_args[1] == "-f"
    assert str(access_log) in call_args
    assert str(error_log) in call_args


def test_logs_nginx_error_flag(mock_nginx_enabled, tmp_path, mocker):
    """``--error`` flag tails only the error log."""
    domain = "myapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    error_log.touch()

    _write_nginx_config(mock_nginx_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")
    mock_popen = mocker.patch("subprocess.Popen")
    mock_popen.return_value.wait.return_value = 0

    result = runner.invoke(app, ["logs", domain, "--error"])

    assert result.exit_code == 0
    call_args = mock_popen.call_args[0][0]
    assert str(error_log) in call_args
    assert str(access_log) not in call_args


def test_logs_nginx_access_flag(mock_nginx_enabled, tmp_path, mocker):
    """``--access`` flag tails only the access log."""
    domain = "myapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    error_log.touch()

    _write_nginx_config(mock_nginx_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")
    mock_popen = mocker.patch("subprocess.Popen")
    mock_popen.return_value.wait.return_value = 0

    result = runner.invoke(app, ["logs", domain, "--access"])

    assert result.exit_code == 0
    call_args = mock_popen.call_args[0][0]
    assert str(access_log) in call_args
    assert str(error_log) not in call_args


# ---------------------------------------------------------------------------
# Happy-path tests — Apache
# ---------------------------------------------------------------------------


def test_logs_apache_both_streams(mock_apache_enabled, tmp_path, mocker):
    """Default invocation tails both access and error logs for an Apache vhost."""
    domain = "apacheapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    error_log.touch()

    _write_apache_config(mock_apache_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")
    mock_popen = mocker.patch("subprocess.Popen")
    mock_popen.return_value.wait.return_value = 0

    result = runner.invoke(app, ["logs", domain])

    assert result.exit_code == 0
    call_args = mock_popen.call_args[0][0]
    assert str(access_log) in call_args
    assert str(error_log) in call_args


def test_logs_apache_error_flag(mock_apache_enabled, tmp_path, mocker):
    """``--error`` flag tails only the Apache error log."""
    domain = "apacheapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    error_log.touch()

    _write_apache_config(mock_apache_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")
    mock_popen = mocker.patch("subprocess.Popen")
    mock_popen.return_value.wait.return_value = 0

    result = runner.invoke(app, ["logs", domain, "--error"])

    assert result.exit_code == 0
    call_args = mock_popen.call_args[0][0]
    assert str(error_log) in call_args
    assert str(access_log) not in call_args


def test_logs_apache_access_flag(mock_apache_enabled, tmp_path, mocker):
    """``--access`` flag tails only the Apache access log."""
    domain = "apacheapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    error_log.touch()

    _write_apache_config(mock_apache_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")
    mock_popen = mocker.patch("subprocess.Popen")
    mock_popen.return_value.wait.return_value = 0

    result = runner.invoke(app, ["logs", domain, "--access"])

    assert result.exit_code == 0
    call_args = mock_popen.call_args[0][0]
    assert str(access_log) in call_args
    assert str(error_log) not in call_args


# ---------------------------------------------------------------------------
# Provider override (--provider)
# ---------------------------------------------------------------------------


def test_logs_provider_override_apache(mock_apache_enabled, tmp_path, mocker):
    """``--provider apache`` forces Apache even when not auto-detected."""
    domain = "override.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    error_log.touch()

    _write_apache_config(mock_apache_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")
    mock_popen = mocker.patch("subprocess.Popen")
    mock_popen.return_value.wait.return_value = 0

    result = runner.invoke(app, ["logs", domain, "--provider", "apache"])

    assert result.exit_code == 0
    call_args = mock_popen.call_args[0][0]
    assert str(access_log) in call_args


# ---------------------------------------------------------------------------
# SIGINT / KeyboardInterrupt exits cleanly with code 0
# ---------------------------------------------------------------------------


def test_logs_sigint_exits_cleanly(mock_nginx_enabled, tmp_path, mocker):
    """KeyboardInterrupt during tail.wait() exits with code 0, no traceback."""
    domain = "myapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    error_log.touch()

    _write_nginx_config(mock_nginx_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")
    mock_popen = mocker.patch("subprocess.Popen")
    mock_proc = mock_popen.return_value
    mock_proc.wait.side_effect = KeyboardInterrupt

    result = runner.invoke(app, ["logs", domain])

    assert result.exit_code == 0
    mock_proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# Error conditions
# ---------------------------------------------------------------------------


def test_logs_vhost_not_found(mock_nginx_enabled, mocker):
    """Non-existent domain exits 1 with 'VHost not found or disabled' message."""
    result = runner.invoke(app, ["logs", "nonexistent.test"])

    assert result.exit_code == 1
    assert "VHost not found or disabled: 'nonexistent.test'" in result.stdout


def test_logs_vhost_disabled_nginx(mock_nginx_enabled, tmp_path, mocker):
    """Domain in sites-available but NOT in sites-enabled exits 1."""
    domain = "disabled.test"
    available = tmp_path / "nginx-avail"
    available.mkdir(exist_ok=True)
    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
    (available / f"{domain}.conf").write_text("server {}\n")
    # nginx-enabled is empty → not enabled

    result = runner.invoke(app, ["logs", domain])

    assert result.exit_code == 1
    assert "VHost not found or disabled" in result.stdout


def test_logs_no_log_paths_in_config(mock_nginx_enabled, mocker):
    """Config exists but has no log directives → 'No log paths found' error."""
    domain = "nologs.test"
    conf = mock_nginx_enabled / f"{domain}.conf"
    conf.write_text("server {\n    listen 80;\n    server_name nologs.test;\n}\n")

    result = runner.invoke(app, ["logs", domain])

    assert result.exit_code == 1
    assert f"No log paths found in configuration for '{domain}'" in result.stdout


def test_logs_access_log_file_missing(mock_nginx_enabled, tmp_path, mocker):
    """Config has log paths but access log file does not exist → exit 1."""
    domain = "myapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    # Intentionally do NOT create access_log
    error_log.touch()

    _write_nginx_config(mock_nginx_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")

    result = runner.invoke(app, ["logs", domain])

    assert result.exit_code == 1
    assert "Log file not found at" in result.stdout
    assert str(access_log) in result.stdout


def test_logs_error_log_file_missing_with_error_flag(mock_nginx_enabled, tmp_path, mocker):
    """``--error`` flag targets a non-existent error log file → exit 1."""
    domain = "myapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    # Intentionally do NOT create error_log

    _write_nginx_config(mock_nginx_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")

    result = runner.invoke(app, ["logs", domain, "--error"])

    assert result.exit_code == 1
    assert "Log file not found at" in result.stdout
    assert str(error_log) in result.stdout


def test_logs_mutually_exclusive_flags(mock_nginx_enabled):
    """``--error`` and ``--access`` together → exit 1, mutual exclusion message."""
    result = runner.invoke(app, ["logs", "myapp.test", "--error", "--access"])

    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


def test_logs_tail_binary_not_found(mock_nginx_enabled, tmp_path, mocker):
    """``tail`` absent from PATH → exit 1, message contains 'tail' binary not found."""
    domain = "myapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    error_log.touch()

    _write_nginx_config(mock_nginx_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value=None)

    result = runner.invoke(app, ["logs", domain])

    assert result.exit_code == 1
    assert "'tail' binary not found on PATH" in result.stdout


# ---------------------------------------------------------------------------
# Popen shell=False assertion
# ---------------------------------------------------------------------------


def test_logs_popen_uses_shell_false(mock_nginx_enabled, tmp_path, mocker):
    """The tail subprocess is always launched with shell=False."""
    domain = "myapp.test"
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    access_log.touch()
    error_log.touch()

    _write_nginx_config(mock_nginx_enabled, domain, str(access_log), str(error_log))

    mocker.patch("shutil.which", return_value="/usr/bin/tail")
    mock_popen = mocker.patch("subprocess.Popen")
    mock_popen.return_value.wait.return_value = 0

    runner.invoke(app, ["logs", domain])

    call_kwargs = mock_popen.call_args[1]
    assert call_kwargs.get("shell") is False or "shell" not in call_kwargs or call_kwargs["shell"] is False
