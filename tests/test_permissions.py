"""
Tests for ULTIMATE_VHOST-022 — Automated Webroot Permission & SELinux Hardening.

Covers:
- Feature 1: resolve_webserver_user_group for all 4 distribution × provider combos
- Feature 2: apply_webroot_permissions command sequence (ordered, rollback-gated)
- Feature 3: CLI flags (--webroot-user, --webroot-group, --webroot-perms, --skip-permissions)
- Feature 4: SELinux context hardening (semanage+restorecon, chcon fallback, OS-gated)
- Feature 5: Integration into create workflow (permission & SELinux steps hooked in correctly)
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call

from typer.testing import CliRunner

from vhost_helper.permissions import (
    resolve_webserver_user_group,
    get_current_user,
    validate_webroot_perms,
    apply_webroot_permissions,
    is_selinux_active,
    apply_selinux_webroot_context,
)
from vhost_helper.models import ServerType
from vhost_helper.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Feature 1 — resolve_webserver_user_group
# ---------------------------------------------------------------------------


class TestResolveWebserverUserGroup:
    def test_debian_nginx(self):
        user, group = resolve_webserver_user_group("debian_family", ServerType.NGINX)
        assert user == "www-data"
        assert group == "www-data"

    def test_debian_apache(self):
        user, group = resolve_webserver_user_group("debian_family", ServerType.APACHE)
        assert user == "www-data"
        assert group == "www-data"

    def test_rhel_nginx(self):
        user, group = resolve_webserver_user_group("rhel_family", ServerType.NGINX)
        assert user == "nginx"
        assert group == "nginx"

    def test_rhel_apache(self):
        user, group = resolve_webserver_user_group("rhel_family", ServerType.APACHE)
        assert user == "apache"
        assert group == "apache"

    def test_unknown_family_falls_back_to_www_data(self):
        user, group = resolve_webserver_user_group("unknown", ServerType.NGINX)
        assert user == "www-data"
        assert group == "www-data"

    def test_unknown_family_apache_falls_back(self):
        user, group = resolve_webserver_user_group("unknown", ServerType.APACHE)
        assert user == "www-data"
        assert group == "www-data"


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    def test_returns_user_env_var(self):
        with patch.dict("os.environ", {"USER": "alice"}, clear=False):
            assert get_current_user() == "alice"

    def test_falls_back_to_logname(self):
        env = {"LOGNAME": "bob"}
        with patch.dict("os.environ", env, clear=False):
            # Remove USER if set
            import os

            original = os.environ.pop("USER", None)
            try:
                assert get_current_user() == "bob"
            finally:
                if original is not None:
                    os.environ["USER"] = original

    def test_falls_back_to_os_getlogin(self):
        import os

        env_bak = os.environ.copy()
        os.environ.pop("USER", None)
        os.environ.pop("LOGNAME", None)
        try:
            with patch("os.getlogin", return_value="charlie"):
                assert get_current_user() == "charlie"
        finally:
            os.environ.clear()
            os.environ.update(env_bak)

    def test_falls_back_to_root_on_os_error(self):
        import os

        env_bak = os.environ.copy()
        os.environ.pop("USER", None)
        os.environ.pop("LOGNAME", None)
        try:
            with patch("os.getlogin", side_effect=OSError("no tty")):
                assert get_current_user() == "root"
        finally:
            os.environ.clear()
            os.environ.update(env_bak)


# ---------------------------------------------------------------------------
# Feature 3 — validate_webroot_perms
# ---------------------------------------------------------------------------


class TestValidateWebrootPerms:
    def test_valid_755_644(self):
        assert validate_webroot_perms("755:644") == ("755", "644")

    def test_valid_750_640(self):
        assert validate_webroot_perms("750:640") == ("750", "640")

    def test_valid_700_600(self):
        assert validate_webroot_perms("700:600") == ("700", "600")

    def test_invalid_format_missing_colon(self):
        with pytest.raises(ValueError, match="format"):
            validate_webroot_perms("755644")

    def test_invalid_format_too_short(self):
        with pytest.raises(ValueError, match="format"):
            validate_webroot_perms("75:64")

    def test_invalid_format_letters(self):
        with pytest.raises(ValueError, match="format"):
            validate_webroot_perms("abc:xyz")

    def test_invalid_non_octal_digit_8(self):
        with pytest.raises(ValueError, match="octal"):
            validate_webroot_perms("758:644")

    def test_invalid_non_octal_digit_9(self):
        with pytest.raises(ValueError, match="octal"):
            validate_webroot_perms("755:649")


# ---------------------------------------------------------------------------
# Feature 2 — apply_webroot_permissions
# ---------------------------------------------------------------------------


class TestApplyWebrootPermissions:
    @patch("vhost_helper.permissions.run_elevated_command")
    @patch("vhost_helper.permissions.get_sudo_prefix", return_value=[])
    def test_calls_four_commands_in_order(self, mock_sudo, mock_run):
        path = Path("/var/www/mysite")
        apply_webroot_permissions(path, "alice", "www-data")

        assert mock_run.call_count == 4
        calls = mock_run.call_args_list

        # 1. chown
        assert calls[0] == call(["chown", "-R", "alice:www-data", "/var/www/mysite"])
        # 2. chmod dirs
        assert calls[1] == call(
            ["find", "/var/www/mysite", "-type", "d", "-exec", "chmod", "755", "{}", "+"]
        )
        # 3. chmod files
        assert calls[2] == call(
            ["find", "/var/www/mysite", "-type", "f", "-exec", "chmod", "644", "{}", "+"]
        )
        # 4. SetGID
        assert calls[3] == call(
            ["find", "/var/www/mysite", "-type", "d", "-exec", "chmod", "g+s", "{}", "+"]
        )

    @patch("vhost_helper.permissions.run_elevated_command")
    @patch("vhost_helper.permissions.get_sudo_prefix", return_value=[])
    def test_custom_modes_passed_correctly(self, mock_sudo, mock_run):
        path = Path("/var/www/restricted")
        apply_webroot_permissions(path, "bob", "apache", dir_mode="750", file_mode="640")

        calls = mock_run.call_args_list
        assert calls[1] == call(
            ["find", "/var/www/restricted", "-type", "d", "-exec", "chmod", "750", "{}", "+"]
        )
        assert calls[2] == call(
            ["find", "/var/www/restricted", "-type", "f", "-exec", "chmod", "640", "{}", "+"]
        )

    @patch(
        "vhost_helper.permissions.run_elevated_command",
        side_effect=RuntimeError("chown failed"),
    )
    @patch("vhost_helper.permissions.get_sudo_prefix", return_value=[])
    def test_raises_on_first_command_failure(self, mock_sudo, mock_run):
        with pytest.raises(RuntimeError, match="chown failed"):
            apply_webroot_permissions(Path("/tmp/x"), "u", "g")

    @patch("vhost_helper.permissions.get_sudo_prefix", return_value=["sudo"])
    @patch("vhost_helper.permissions.run_elevated_command")
    def test_sudo_prefix_prepended(self, mock_run, mock_sudo):
        apply_webroot_permissions(Path("/var/www/site"), "u", "g")
        first_call_cmd = mock_run.call_args_list[0][0][0]
        assert first_call_cmd[0] == "sudo"


# ---------------------------------------------------------------------------
# Feature 4 — is_selinux_active
# ---------------------------------------------------------------------------


class TestIsSelinuxActive:
    def test_returns_false_when_getenforce_not_found(self):
        with patch("shutil.which", return_value=None):
            assert is_selinux_active() is False

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/sbin/getenforce")
    def test_returns_true_when_enforcing(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(stdout="Enforcing", returncode=0)
        assert is_selinux_active() is True

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/sbin/getenforce")
    def test_returns_true_when_permissive(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(stdout="Permissive", returncode=0)
        assert is_selinux_active() is True

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/sbin/getenforce")
    def test_returns_false_when_disabled(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(stdout="Disabled", returncode=0)
        assert is_selinux_active() is False

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/sbin/getenforce")
    def test_returns_false_when_stdout_empty(self, mock_which, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        assert is_selinux_active() is False

    @patch(
        "subprocess.run", side_effect=Exception("unexpected")
    )
    @patch("shutil.which", return_value="/usr/sbin/getenforce")
    def test_returns_false_on_exception(self, mock_which, mock_run):
        assert is_selinux_active() is False


# ---------------------------------------------------------------------------
# Feature 4 — apply_selinux_webroot_context
# ---------------------------------------------------------------------------


class TestApplySelinuxWebrootContext:
    @patch("vhost_helper.permissions.run_elevated_command")
    @patch("vhost_helper.permissions.get_sudo_prefix", return_value=[])
    @patch("shutil.which", return_value="/usr/sbin/semanage")
    def test_uses_semanage_and_restorecon_when_available(
        self, mock_which, mock_sudo, mock_run
    ):
        path = Path("/var/www/mysite")
        apply_selinux_webroot_context(path)

        calls = mock_run.call_args_list
        assert len(calls) == 2
        # semanage fcontext
        assert calls[0] == call(
            [
                "semanage",
                "fcontext",
                "-a",
                "-t",
                "httpd_sys_content_t",
                "/var/www/mysite(/.*)?",
            ]
        )
        # restorecon
        assert calls[1] == call(["restorecon", "-Rv", "/var/www/mysite"])

    @patch("vhost_helper.permissions.run_elevated_command")
    @patch("vhost_helper.permissions.get_sudo_prefix", return_value=[])
    @patch("shutil.which", return_value=None)
    def test_falls_back_to_chcon_when_semanage_absent(
        self, mock_which, mock_sudo, mock_run
    ):
        path = Path("/var/www/mysite")
        apply_selinux_webroot_context(path)

        assert mock_run.call_count == 1
        assert mock_run.call_args == call(
            ["chcon", "-Rt", "httpd_sys_content_t", "/var/www/mysite"]
        )

    @patch(
        "vhost_helper.permissions.run_elevated_command",
        side_effect=[RuntimeError("semanage failed"), None],
    )
    @patch("vhost_helper.permissions.get_sudo_prefix", return_value=[])
    @patch("shutil.which", return_value="/usr/sbin/semanage")
    def test_falls_back_to_chcon_when_semanage_fails(
        self, mock_which, mock_sudo, mock_run
    ):
        """semanage present but fails → chcon fallback should succeed."""
        path = Path("/var/www/mysite")
        apply_selinux_webroot_context(path)

        assert mock_run.call_count == 2
        last_cmd = mock_run.call_args_list[1][0][0]
        assert last_cmd == ["chcon", "-Rt", "httpd_sys_content_t", "/var/www/mysite"]

    @patch(
        "vhost_helper.permissions.run_elevated_command",
        side_effect=RuntimeError("chcon failed"),
    )
    @patch("vhost_helper.permissions.get_sudo_prefix", return_value=[])
    @patch("shutil.which", return_value=None)
    def test_raises_when_chcon_fails(self, mock_which, mock_sudo, mock_run):
        with pytest.raises(RuntimeError, match="SELinux context application failed"):
            apply_selinux_webroot_context(Path("/var/www/mysite"))

    @patch(
        "vhost_helper.permissions.run_elevated_command",
        side_effect=[RuntimeError("semanage"), RuntimeError("chcon failed")],
    )
    @patch("vhost_helper.permissions.get_sudo_prefix", return_value=[])
    @patch("shutil.which", return_value="/usr/sbin/semanage")
    def test_raises_when_both_semanage_and_chcon_fail(
        self, mock_which, mock_sudo, mock_run
    ):
        with pytest.raises(RuntimeError, match="SELinux context application failed"):
            apply_selinux_webroot_context(Path("/var/www/mysite"))


# ---------------------------------------------------------------------------
# Feature 3 + 5 — CLI integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_create_setup(mocker, tmp_path):
    """Fixture providing a fully mocked environment for the create command."""
    available = tmp_path / "nginx-available"
    enabled = tmp_path / "nginx-enabled"
    available.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)

    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)

    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")

    return available, enabled, tmp_path


def test_create_applies_permissions_by_default(mock_create_setup, mocker):
    """Default create should call apply_webroot_permissions."""
    _, _, tmp_path = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    mock_apply = mocker.patch("vhost_helper.main.apply_webroot_permissions")
    mocker.patch("vhost_helper.main.get_current_user", return_value="alice")
    mocker.patch(
        "vhost_helper.main.resolve_webserver_user_group", return_value=("nginx", "nginx")
    )
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=False)
    mocker.patch("vhost_helper.main.get_os_info", return_value=MagicMock(family="debian_family"))

    result = runner.invoke(app, ["create", "test.local", str(doc_root)])
    assert result.exit_code == 0
    mock_apply.assert_called_once_with(
        doc_root.absolute(), "alice", "nginx", dir_mode="755", file_mode="644"
    )


def test_create_skip_permissions_bypasses_all_perm_steps(mock_create_setup, mocker):
    """--skip-permissions must skip chown/chmod/selinux entirely."""
    _, _, tmp_path = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    mock_apply = mocker.patch("vhost_helper.main.apply_webroot_permissions")
    mock_selinux = mocker.patch("vhost_helper.main.apply_selinux_webroot_context")
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=True)

    result = runner.invoke(
        app, ["create", "test.local", str(doc_root), "--skip-permissions"]
    )
    assert result.exit_code == 0
    mock_apply.assert_not_called()
    mock_selinux.assert_not_called()


def test_create_skip_permissions_with_webroot_user_exits_1(mock_create_setup, tmp_path):
    """--skip-permissions + --webroot-user must fail with exit code 1."""
    _, _, tmp_path2 = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(
        app,
        [
            "create",
            "test.local",
            str(doc_root),
            "--skip-permissions",
            "--webroot-user",
            "alice",
        ],
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


def test_create_skip_permissions_with_webroot_group_exits_1(mock_create_setup, tmp_path):
    """--skip-permissions + --webroot-group must fail with exit code 1."""
    _, _, _ = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(
        app,
        [
            "create",
            "test.local",
            str(doc_root),
            "--skip-permissions",
            "--webroot-group",
            "mygroup",
        ],
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


def test_create_skip_permissions_with_webroot_perms_exits_1(mock_create_setup, tmp_path):
    """--skip-permissions + --webroot-perms must fail with exit code 1."""
    _, _, _ = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(
        app,
        [
            "create",
            "test.local",
            str(doc_root),
            "--skip-permissions",
            "--webroot-perms",
            "750:640",
        ],
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


def test_create_invalid_webroot_perms_format_exits_1(mock_create_setup, tmp_path):
    """Invalid --webroot-perms must fail before any filesystem changes."""
    _, _, _ = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(
        app,
        ["create", "test.local", str(doc_root), "--webroot-perms", "abc:xyz"],
    )
    assert result.exit_code == 1
    assert "Invalid" in result.stdout or "format" in result.stdout


def test_create_webroot_perms_override_applied(mock_create_setup, mocker, tmp_path):
    """--webroot-perms 750:640 must pass those modes to apply_webroot_permissions."""
    _, _, tmp_path2 = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    mock_apply = mocker.patch("vhost_helper.main.apply_webroot_permissions")
    mocker.patch("vhost_helper.main.get_current_user", return_value="alice")
    mocker.patch(
        "vhost_helper.main.resolve_webserver_user_group", return_value=("www-data", "www-data")
    )
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=False)
    mocker.patch(
        "vhost_helper.main.get_os_info", return_value=MagicMock(family="debian_family")
    )

    result = runner.invoke(
        app,
        ["create", "test.local", str(doc_root), "--webroot-perms", "750:640"],
    )
    assert result.exit_code == 0
    mock_apply.assert_called_once_with(
        doc_root.absolute(), "alice", "www-data", dir_mode="750", file_mode="640"
    )


def test_create_webroot_user_override(mock_create_setup, mocker, tmp_path):
    """--webroot-user overrides the chown owner."""
    _, _, _ = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    mock_apply = mocker.patch("vhost_helper.main.apply_webroot_permissions")
    mocker.patch(
        "vhost_helper.main.resolve_webserver_user_group", return_value=("www-data", "www-data")
    )
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=False)
    mocker.patch(
        "vhost_helper.main.get_os_info", return_value=MagicMock(family="debian_family")
    )

    result = runner.invoke(
        app,
        ["create", "test.local", str(doc_root), "--webroot-user", "deploy"],
    )
    assert result.exit_code == 0
    call_args = mock_apply.call_args
    assert call_args[0][1] == "deploy"


def test_create_webroot_group_override(mock_create_setup, mocker, tmp_path):
    """--webroot-group overrides the chown group."""
    _, _, _ = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    mock_apply = mocker.patch("vhost_helper.main.apply_webroot_permissions")
    mocker.patch("vhost_helper.main.get_current_user", return_value="alice")
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=False)
    mocker.patch(
        "vhost_helper.main.get_os_info", return_value=MagicMock(family="debian_family")
    )

    result = runner.invoke(
        app,
        ["create", "test.local", str(doc_root), "--webroot-group", "customgrp"],
    )
    assert result.exit_code == 0
    call_args = mock_apply.call_args
    assert call_args[0][2] == "customgrp"


def test_create_selinux_applied_on_rhel_with_active_selinux(
    mock_create_setup, mocker, tmp_path
):
    """On rhel_family with SELinux active, apply_selinux_webroot_context must be called."""
    _, _, _ = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    mocker.patch("vhost_helper.main.apply_webroot_permissions")
    mocker.patch("vhost_helper.main.get_current_user", return_value="alice")
    mocker.patch(
        "vhost_helper.main.resolve_webserver_user_group", return_value=("nginx", "nginx")
    )
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=True)
    mocker.patch(
        "vhost_helper.main.get_os_info", return_value=MagicMock(family="rhel_family")
    )
    mock_selinux = mocker.patch("vhost_helper.main.apply_selinux_webroot_context")

    result = runner.invoke(app, ["create", "test.local", str(doc_root)])
    assert result.exit_code == 0
    mock_selinux.assert_called_once_with(doc_root.absolute())


def test_create_selinux_not_applied_on_debian(mock_create_setup, mocker, tmp_path):
    """On debian_family, apply_selinux_webroot_context must NOT be called."""
    _, _, _ = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    mocker.patch("vhost_helper.main.apply_webroot_permissions")
    mocker.patch("vhost_helper.main.get_current_user", return_value="alice")
    mocker.patch(
        "vhost_helper.main.resolve_webserver_user_group", return_value=("www-data", "www-data")
    )
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=True)
    mocker.patch(
        "vhost_helper.main.get_os_info", return_value=MagicMock(family="debian_family")
    )
    mock_selinux = mocker.patch("vhost_helper.main.apply_selinux_webroot_context")

    result = runner.invoke(app, ["create", "test.local", str(doc_root)])
    assert result.exit_code == 0
    mock_selinux.assert_not_called()


def test_create_selinux_not_applied_when_selinux_disabled(
    mock_create_setup, mocker, tmp_path
):
    """On rhel_family but SELinux Disabled, apply_selinux_webroot_context must NOT be called."""
    _, _, _ = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    mocker.patch("vhost_helper.main.apply_webroot_permissions")
    mocker.patch("vhost_helper.main.get_current_user", return_value="alice")
    mocker.patch(
        "vhost_helper.main.resolve_webserver_user_group", return_value=("nginx", "nginx")
    )
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=False)
    mocker.patch(
        "vhost_helper.main.get_os_info", return_value=MagicMock(family="rhel_family")
    )
    mock_selinux = mocker.patch("vhost_helper.main.apply_selinux_webroot_context")

    result = runner.invoke(app, ["create", "test.local", str(doc_root)])
    assert result.exit_code == 0
    mock_selinux.assert_not_called()


def test_permission_failure_triggers_rollback(mock_create_setup, mocker, tmp_path):
    """A RuntimeError from apply_webroot_permissions must trigger full rollback."""
    _, _, _ = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    mocker.patch(
        "vhost_helper.main.apply_webroot_permissions",
        side_effect=RuntimeError("chown failed"),
    )
    mocker.patch("vhost_helper.main.get_current_user", return_value="alice")
    mocker.patch(
        "vhost_helper.main.resolve_webserver_user_group", return_value=("www-data", "www-data")
    )
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=False)
    mocker.patch(
        "vhost_helper.main.get_os_info", return_value=MagicMock(family="debian_family")
    )
    mock_remove_entry = mocker.patch("vhost_helper.main.remove_entry")
    mock_remove_vhost = mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.remove_vhost"
    )

    result = runner.invoke(app, ["create", "test.local", str(doc_root)])
    assert result.exit_code == 1

    # Both hostfile and vhost config should be rolled back
    mock_remove_entry.assert_called()
    mock_remove_vhost.assert_called()


def test_selinux_failure_triggers_rollback(mock_create_setup, mocker, tmp_path):
    """A RuntimeError from apply_selinux_webroot_context must trigger full rollback."""
    _, _, _ = mock_create_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    mocker.patch("vhost_helper.main.apply_webroot_permissions")
    mocker.patch("vhost_helper.main.get_current_user", return_value="alice")
    mocker.patch(
        "vhost_helper.main.resolve_webserver_user_group", return_value=("nginx", "nginx")
    )
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=True)
    mocker.patch(
        "vhost_helper.main.get_os_info", return_value=MagicMock(family="rhel_family")
    )
    mocker.patch(
        "vhost_helper.main.apply_selinux_webroot_context",
        side_effect=RuntimeError("chcon failed"),
    )
    mock_remove_entry = mocker.patch("vhost_helper.main.remove_entry")
    mock_remove_vhost = mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.remove_vhost"
    )

    result = runner.invoke(app, ["create", "test.local", str(doc_root)])
    assert result.exit_code == 1
    mock_remove_entry.assert_called()
    mock_remove_vhost.assert_called()
