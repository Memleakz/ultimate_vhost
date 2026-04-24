"""
QA Phase 3 — Additions for ULTIMATE_VHOST-003 service-state awareness.

Covers:
- BUG-001 regression: remove_vhost RuntimeError propagation when nginx stopped
- BUG-002: is_nginx_running timeout / SubprocessError coverage
- Coverage gaps: port boundaries, symlink-already-exists, create rollback, document root is-file
- Remove command service-state awareness (new behaviour added with bug fix)
"""

import subprocess
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vhost_helper.main import app
from vhost_helper.models import VHostConfig, ServerType
from vhost_helper.providers.nginx import NginxProvider, is_nginx_running

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_nginx_dirs(mocker):
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        available = tmp_path / "sites-available"
        enabled = tmp_path / "sites-enabled"
        available.mkdir()
        enabled.mkdir()
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)
        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
        yield available, enabled, tmp_path


@pytest.fixture
def doc_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# BUG-001 Regression: remove_vhost RuntimeError propagation
# ---------------------------------------------------------------------------


def test_remove_vhost_skips_reload_when_service_stopped(tmp_nginx_dirs, mocker):
    """BUG-001 fix: remove_vhost(service_running=False) must NOT call reload()."""
    available, enabled, tmp_path = tmp_nginx_dirs
    mocker.patch("subprocess.run")
    mock_reload = mocker.patch.object(NginxProvider, "reload")

    provider = NginxProvider()
    provider.remove_vhost("some.test", service_running=False)

    mock_reload.assert_not_called()


def test_remove_vhost_calls_reload_when_service_running(tmp_nginx_dirs, mocker):
    """Existing behaviour preserved: reload IS called when service_running=True."""
    available, enabled, tmp_path = tmp_nginx_dirs
    mocker.patch("subprocess.run")
    mock_reload = mocker.patch.object(NginxProvider, "reload")

    provider = NginxProvider()
    provider.remove_vhost("some.test", service_running=True)

    mock_reload.assert_called_once()


def test_remove_vhost_does_not_raise_from_stopped_service(tmp_nginx_dirs, mocker):
    """BUG-001 fix: remove_vhost with stopped service must not raise RuntimeError."""
    available, enabled, tmp_path = tmp_nginx_dirs
    mocker.patch("subprocess.run")

    # Reload would have raised RuntimeError before the fix
    mocker.patch.object(
        NginxProvider, "reload", side_effect=RuntimeError("reload failed")
    )

    provider = NginxProvider()
    # Should NOT raise — reload is not called when service_running=False
    provider.remove_vhost("some.test", service_running=False)


def test_cli_remove_succeeds_when_service_stopped(tmp_nginx_dirs, doc_root, mocker):
    """BUG-001 fix: `vhost remove` exits 0 and shows skipped-reload message when nginx stopped."""
    available, _, _ = tmp_nginx_dirs
    (available / "stopped.test.conf").touch()
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch.object(NginxProvider, "remove_vhost")

    result = runner.invoke(app, ["remove", "stopped.test", "--force"])

    assert result.exit_code == 0, result.output
    assert "Skipped" in result.output
    assert "Nginx reload" in result.output
    assert "Notification: Nginx is installed but not running" in result.output


def test_cli_remove_succeeds_when_service_running(tmp_nginx_dirs, doc_root, mocker):
    """Existing remove happy path unchanged: reload message shown, no warning."""
    available, _, _ = tmp_nginx_dirs
    (available / "running.test.conf").touch()
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch.object(NginxProvider, "remove_vhost")

    result = runner.invoke(app, ["remove", "running.test", "--force"])

    assert result.exit_code == 0, result.output
    assert "reloaded successfully" in result.output
    assert "Notification" not in result.output


# ---------------------------------------------------------------------------
# BUG-002: is_nginx_running timeout / SubprocessError
# ---------------------------------------------------------------------------


def test_is_nginx_running_returns_false_on_timeout(mocker):
    """BUG-002 fix: TimeoutExpired must be caught and return False, not raise."""
    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["systemctl"], timeout=5),
    )
    assert is_nginx_running() is False


def test_is_nginx_running_returns_false_on_subprocess_error(mocker):
    """SubprocessError (base class) is also handled gracefully."""
    mocker.patch("subprocess.run", side_effect=subprocess.SubprocessError)
    assert is_nginx_running() is False


# ---------------------------------------------------------------------------
# Coverage gap: port boundary values
# ---------------------------------------------------------------------------


def test_vhost_config_port_boundary_min_valid(tmp_path):
    """Port=1 is the minimum valid value."""
    config = VHostConfig(
        domain="boundary.test",
        document_root=tmp_path,
        port=1,
        server_type=ServerType.NGINX,
    )
    assert config.port == 1


def test_vhost_config_port_boundary_max_valid(tmp_path):
    """Port=65535 is the maximum valid value."""
    config = VHostConfig(
        domain="boundary.test",
        document_root=tmp_path,
        port=65535,
        server_type=ServerType.NGINX,
    )
    assert config.port == 65535


# ---------------------------------------------------------------------------
# Coverage gap: document root is a file, not a directory
# ---------------------------------------------------------------------------


def test_cli_create_document_root_is_file(mocker, tmp_path):
    """If document_root is a file (not a directory), the command must fail."""
    file_path = tmp_path / "notadir.txt"
    file_path.write_text("data")

    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)

    result = runner.invoke(app, ["create", "file.test", str(file_path)])

    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Coverage gap: create_vhost when symlink already exists
# ---------------------------------------------------------------------------


def test_create_vhost_skips_ln_if_symlink_already_exists(tmp_nginx_dirs, mocker):
    """If the enabled symlink already exists, the ln command must NOT be run again."""
    available, enabled, tmp_path = tmp_nginx_dirs

    # Pre-create the symlink so enabled_link.exists() returns True (.conf extension required)
    fake_target = available / "existing.test.conf"
    fake_target.touch()
    link = enabled / "existing.test.conf"
    link.symlink_to(fake_target)

    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")
    mocker.patch.object(NginxProvider, "validate_config", return_value=True)
    mocker.patch.object(NginxProvider, "reload")

    provider = NginxProvider()
    config = VHostConfig(
        domain="existing.test",
        document_root=tmp_path,
        port=80,
        server_type=ServerType.NGINX,
    )
    provider.create_vhost(config, service_running=True)

    calls = [" ".join(str(c) for c in call[0][0]) for call in mock_run.call_args_list]
    assert not any(
        "ln" in c for c in calls
    ), "ln should NOT be called when symlink already exists"


# ---------------------------------------------------------------------------
# Coverage gap: create command rollback when NginxProvider.create_vhost raises
# ---------------------------------------------------------------------------


def test_cli_create_rollback_removes_hostfile_entry_on_nginx_failure(
    tmp_nginx_dirs, doc_root, mocker
):
    """If create_vhost raises, the hostfile entry added earlier must be rolled back."""
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)

    mock_add = mocker.patch("vhost_helper.main.add_entry")
    mock_remove = mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch.object(
        NginxProvider, "create_vhost", side_effect=RuntimeError("nginx boom")
    )

    result = runner.invoke(app, ["create", "rollback.test", str(doc_root)])

    assert result.exit_code == 1
    assert "nginx boom" in result.output
    # add_entry is called for both the domain and its www counterpart
    assert mock_add.call_count == 2
    # remove_entry is called for both domain and www counterpart in rollback
    assert mock_remove.call_count == 2
    mock_remove.assert_any_call("rollback.test")


# ---------------------------------------------------------------------------
# Coverage gap: domain validation edge cases
# ---------------------------------------------------------------------------


def test_domain_validation_exactly_253_chars(mocker):
    """A 253-character domain must pass format validation (only document root fails)."""
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    domain = "a" * 248 + ".test"  # 253 chars total
    assert len(domain) == 253

    result = runner.invoke(app, ["create", domain, "/nonexistent"])
    # Should NOT be rejected with a domain format error
    assert "Invalid domain format" not in result.output
    assert "too long" not in result.output


def test_domain_validation_254_chars_rejected():
    """A 254-character domain must be rejected at validation (before nginx checks)."""
    domain = "a" * 249 + ".test"  # 254 chars total
    assert len(domain) == 254

    result = runner.invoke(app, ["create", domain, "/nonexistent"])
    assert result.exit_code == 1
    # "Domain name too long or empty" contains "too long"
    assert "too long" in result.output


def test_domain_starts_with_hyphen_rejected():
    """Domains starting with a hyphen must be rejected (invalid per RFC)."""
    # Use -- separator so Typer doesn't interpret -bad.test as a flag
    result = runner.invoke(app, ["create", "--", "-bad.test", "/tmp"])
    assert result.exit_code == 1
    assert "Invalid domain format" in result.output


def test_domain_double_dot_rejected():
    """Domains containing '..' must be rejected before any nginx check."""
    # Double-dot check happens in validate_domain before nginx is queried
    result = runner.invoke(app, ["create", "bad..test", "/tmp"])
    assert result.exit_code == 1
    assert "double dots" in result.output
