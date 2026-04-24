"""
QA additions for ULTIMATE_VHOST-013 — SELinux Context Handling.

Covers acceptance criteria, error-message wording, edge-cases, and
platform-specificity checks that were absent from the pre-existing suite.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from vhost_helper import os_detector
from vhost_helper.models import VHostConfig, ServerType, RuntimeMode
from vhost_helper.providers.nginx import NginxProvider

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vhost_config(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    return VHostConfig(
        domain="qa013.local",
        document_root=str(doc_root),
        port=80,
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.STATIC,
    )


@pytest.fixture
def patched_provider(mocker, tmp_path):
    """NginxProvider with patched FS paths and elevated-command execution."""
    sites_available = tmp_path / "sites-available"
    sites_enabled = tmp_path / "sites-enabled"
    sites_available.mkdir()
    sites_enabled.mkdir()

    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_available)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "debian_family")

    mock_run = mocker.patch(
        "vhost_helper.providers.nginx.run_elevated_command",
        return_value=subprocess.CompletedProcess([], 0),
    )
    mock_selinux = mocker.patch(
        "vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False
    )

    provider = NginxProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")

    provider.mock_run = mock_run
    provider.mock_selinux = mock_selinux
    provider.sites_available = sites_available

    return provider


# ---------------------------------------------------------------------------
# AC-1 / AC-2: SELinux enforcing → chcon called with correct arguments
# ---------------------------------------------------------------------------


class TestSelinuxContextApplication:
    def test_chcon_called_with_httpd_config_t(self, patched_provider, vhost_config):
        """chcon must use the exact type label 'httpd_config_t'."""
        patched_provider.mock_selinux.return_value = True
        patched_provider.create_vhost(vhost_config)

        chcon_calls = [
            c for c in patched_provider.mock_run.call_args_list if "chcon" in c.args[0]
        ]
        assert chcon_calls, "chcon was not called on SELinux-enforcing system"
        cmd = chcon_calls[0].args[0]
        assert "-t" in cmd
        assert "httpd_config_t" in cmd

    def test_chcon_path_matches_config_path(self, patched_provider, vhost_config):
        """The path passed to chcon must match the sites-available config path."""
        patched_provider.mock_selinux.return_value = True
        patched_provider.create_vhost(vhost_config)

        chcon_calls = [
            c for c in patched_provider.mock_run.call_args_list if "chcon" in c.args[0]
        ]
        cmd = chcon_calls[0].args[0]
        expected_path = str(
            patched_provider.sites_available / (vhost_config.domain + ".conf")
        )
        assert expected_path in cmd

    def test_chcon_executed_before_symlink(self, patched_provider, vhost_config):
        """chcon must be called before creating the symlink (order matters)."""
        patched_provider.mock_selinux.return_value = True
        patched_provider.create_vhost(vhost_config)

        calls = patched_provider.mock_run.call_args_list
        cmd_names = [c.args[0] for c in calls]

        chcon_idx = next(i for i, c in enumerate(cmd_names) if "chcon" in c)
        ln_idx = next(i for i, c in enumerate(cmd_names) if "ln" in c)
        assert chcon_idx < ln_idx, "chcon must be called before ln -s"

    def test_reload_called_after_selinux_context_applied(
        self, patched_provider, vhost_config
    ):
        """Nginx must be reloaded even on SELinux systems when chcon succeeds."""
        patched_provider.mock_selinux.return_value = True
        patched_provider.create_vhost(vhost_config)
        patched_provider.reload.assert_called_once()


# ---------------------------------------------------------------------------
# AC-3: Non-SELinux systems must NOT execute chcon
# ---------------------------------------------------------------------------


class TestSelinuxSkippedOnNonSelinuxSystems:
    def test_chcon_not_called_on_debian_ubuntu(self, patched_provider, vhost_config):
        """Platform without SELinux: chcon must never appear in any command."""
        patched_provider.mock_selinux.return_value = False
        patched_provider.create_vhost(vhost_config)

        chcon_calls = [
            c for c in patched_provider.mock_run.call_args_list if "chcon" in c.args[0]
        ]
        assert not chcon_calls, "chcon must NOT be called on non-SELinux systems"

    def test_no_getenforce_subprocess_when_selinux_absent(self):
        """is_selinux_enforcing must bail out immediately when getenforce is missing."""
        with patch("shutil.which", return_value=None) as mock_which:
            result = os_detector.is_selinux_enforcing()
        assert result is False
        mock_which.assert_called_once_with("getenforce")


# ---------------------------------------------------------------------------
# AC-4: chcon failure → correct error message wording (PRD requirement)
# ---------------------------------------------------------------------------


class TestSelinuxFailureErrorMessage:
    def test_error_message_says_failed_to_apply(
        self, patched_provider, vhost_config, mocker
    ):
        """Error message must say 'Failed to apply SELinux context' (PRD AC-4)."""
        patched_provider.mock_selinux.return_value = True

        def fail_on_chcon(cmd, **kwargs):
            if "chcon" in cmd:
                raise RuntimeError("chcon: permission denied")
            return subprocess.CompletedProcess(cmd, 0)

        patched_provider.mock_run.side_effect = fail_on_chcon
        mocker.patch.object(patched_provider, "remove_vhost")

        with pytest.raises(RuntimeError) as exc_info:
            patched_provider.create_vhost(vhost_config)

        msg = str(exc_info.value)
        assert (
            "Failed to apply SELinux context" in msg
        ), f"Error message must say 'Failed to apply SELinux context', got: {msg!r}"

    def test_error_message_contains_manual_chcon_command(
        self, patched_provider, vhost_config, mocker
    ):
        """Error message must include the manual 'sudo chcon -t httpd_config_t <path>' command."""
        patched_provider.mock_selinux.return_value = True

        def fail_on_chcon(cmd, **kwargs):
            if "chcon" in cmd:
                raise RuntimeError("permission denied")
            return subprocess.CompletedProcess(cmd, 0)

        patched_provider.mock_run.side_effect = fail_on_chcon
        mocker.patch.object(patched_provider, "remove_vhost")

        with pytest.raises(RuntimeError) as exc_info:
            patched_provider.create_vhost(vhost_config)

        msg = str(exc_info.value)
        assert (
            "sudo chcon -t httpd_config_t" in msg
        ), f"Error message must contain 'sudo chcon -t httpd_config_t', got: {msg!r}"

    def test_error_message_contains_config_file_path(
        self, patched_provider, vhost_config, mocker
    ):
        """Error message must include the actual path of the failing config file."""
        patched_provider.mock_selinux.return_value = True

        def fail_on_chcon(cmd, **kwargs):
            if "chcon" in cmd:
                raise RuntimeError("permission denied")
            return subprocess.CompletedProcess(cmd, 0)

        patched_provider.mock_run.side_effect = fail_on_chcon
        mocker.patch.object(patched_provider, "remove_vhost")

        with pytest.raises(RuntimeError) as exc_info:
            patched_provider.create_vhost(vhost_config)

        msg = str(exc_info.value)
        assert (
            vhost_config.domain in msg
        ), f"Error message must contain the domain/path, got: {msg!r}"

    def test_rollback_called_on_chcon_failure(
        self, patched_provider, vhost_config, mocker
    ):
        """remove_vhost must be called with service_running=False on chcon failure."""
        patched_provider.mock_selinux.return_value = True

        def fail_on_chcon(cmd, **kwargs):
            if "chcon" in cmd:
                raise RuntimeError("permission denied")
            return subprocess.CompletedProcess(cmd, 0)

        patched_provider.mock_run.side_effect = fail_on_chcon
        mock_remove = mocker.patch.object(patched_provider, "remove_vhost")

        with pytest.raises(RuntimeError):
            patched_provider.create_vhost(vhost_config)

        mock_remove.assert_called_once_with(vhost_config.domain, service_running=False)

    def test_operation_reported_as_failure_on_chcon_error(
        self, patched_provider, vhost_config, mocker
    ):
        """chcon failure must propagate as RuntimeError (operation failure), not swallowed."""
        patched_provider.mock_selinux.return_value = True

        def fail_on_chcon(cmd, **kwargs):
            if "chcon" in cmd:
                raise RuntimeError("permission denied")
            return subprocess.CompletedProcess(cmd, 0)

        patched_provider.mock_run.side_effect = fail_on_chcon
        mocker.patch.object(patched_provider, "remove_vhost")

        with pytest.raises(RuntimeError):
            patched_provider.create_vhost(vhost_config)


# ---------------------------------------------------------------------------
# Edge cases: is_selinux_enforcing() internals
# ---------------------------------------------------------------------------


class TestIsSelinuxEnforcingEdgeCases:
    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/sbin/getenforce")
    def test_whitespace_only_stdout_returns_false(self, mock_which, mock_run):
        """Whitespace-only getenforce output must not be treated as 'Enforcing'."""
        mock_run.return_value = MagicMock(stdout="   \n  ", returncode=0)
        assert not os_detector.is_selinux_enforcing()

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/sbin/getenforce")
    def test_enforcing_with_trailing_newline_returns_true(self, mock_which, mock_run):
        """'Enforcing\\n' from getenforce must be correctly detected."""
        mock_run.return_value = MagicMock(stdout="Enforcing\n", returncode=0)
        assert os_detector.is_selinux_enforcing()

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/sbin/getenforce")
    def test_mixed_case_output_not_enforcing(self, mock_which, mock_run):
        """Case-sensitive comparison: 'enforcing' (lowercase) must return False."""
        mock_run.return_value = MagicMock(stdout="enforcing", returncode=0)
        assert not os_detector.is_selinux_enforcing()

    @patch("subprocess.run", side_effect=FileNotFoundError("getenforce not found"))
    @patch("shutil.which", return_value="/usr/sbin/getenforce")
    def test_file_not_found_error_returns_false(self, mock_which, mock_run):
        """FileNotFoundError from subprocess must return False gracefully."""
        assert not os_detector.is_selinux_enforcing()

    @patch("subprocess.run")
    @patch("shutil.which", return_value="/usr/sbin/getenforce")
    def test_empty_string_stdout_returns_false(self, mock_which, mock_run):
        """Empty stdout from getenforce must return False (not crash)."""
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        assert not os_detector.is_selinux_enforcing()
