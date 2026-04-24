"""
QA tests for ULTIMATE_VHOST-015 — OS-Aware Nginx Paths & Manual Installation Guide.

Covers:
- detect_os_family() edge cases (IOError mid-read, case-insensitive IDs, amzn, raspbian)
- config.py RHEL vs Debian path selection
- NginxProvider RHEL disable/enable idempotency
- NginxProvider RHEL disable creates conf.disabled dir when absent
- NginxProvider RHEL remove_vhost searches all three paths
- NginxProvider Debian remove_vhost only searches two paths (no NGINX_SITES_DISABLED)
- main.py enable/disable early-exit when already enabled/disabled
- README manual installation and Nginx-only clarification
"""
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

from vhost_helper.os_detector import detect_os_family
from vhost_helper.models import VHostConfig, ServerType, RuntimeMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def vhost_config(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    return VHostConfig(
        domain="qa015.local",
        document_root=str(doc_root),
        port=80,
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.STATIC,
    )


# ---------------------------------------------------------------------------
# detect_os_family() — additional edge cases
# ---------------------------------------------------------------------------

def test_detect_os_family_amzn(tmp_path):
    """Amazon Linux (ID=amzn) should classify as rhel_family."""
    f = tmp_path / "os-release"
    f.write_text("ID=amzn\nVERSION_ID=\"2023\"\n")
    assert detect_os_family(str(f)) == "rhel_family"


def test_detect_os_family_raspbian(tmp_path):
    """Raspbian (ID=raspbian) should classify as debian_family."""
    f = tmp_path / "os-release"
    f.write_text("ID=raspbian\nVERSION_ID=\"11\"\n")
    assert detect_os_family(str(f)) == "debian_family"


def test_detect_os_family_case_insensitive_id(tmp_path):
    """ID values should be compared case-insensitively (ID=UBUNTU → debian_family)."""
    f = tmp_path / "os-release"
    f.write_text("ID=UBUNTU\nVERSION_ID=\"22.04\"\n")
    assert detect_os_family(str(f)) == "debian_family"


def test_detect_os_family_id_like_single_token(tmp_path):
    """ID_LIKE with a single recognised token (e.g. 'rhel') must match."""
    f = tmp_path / "os-release"
    f.write_text("ID=oracle\nID_LIKE=rhel\n")
    assert detect_os_family(str(f)) == "rhel_family"


def test_detect_os_family_ioerror_mid_read(tmp_path):
    """An IOError raised during file read must return 'unknown' without raising."""
    f = tmp_path / "os-release"
    f.write_text("ID=ubuntu\n")
    with patch("builtins.open", side_effect=IOError("disk error")):
        result = detect_os_family(str(f))
    assert result == "unknown"


def test_detect_os_family_malformed_line_ignored(tmp_path):
    """Lines without '=' must be silently skipped."""
    f = tmp_path / "os-release"
    f.write_text("THIS_IS_NOT_VALID\nID=debian\n")
    assert detect_os_family(str(f)) == "debian_family"


def test_detect_os_family_single_quoted_values(tmp_path):
    """Single-quoted values must be stripped correctly."""
    f = tmp_path / "os-release"
    f.write_text("ID='fedora'\nVERSION_ID='39'\n")
    assert detect_os_family(str(f)) == "rhel_family"


def test_detect_os_family_id_like_mixed_families(tmp_path):
    """If ID_LIKE contains both debian and rhel tokens, debian wins (checked first)."""
    f = tmp_path / "os-release"
    # Artificial edge case: ID is unknown, ID_LIKE has both families
    f.write_text("ID=weird\nID_LIKE=\"debian rhel\"\n")
    # debian is checked first in the code
    assert detect_os_family(str(f)) == "debian_family"


# ---------------------------------------------------------------------------
# config.py — RHEL vs Debian path selection
# ---------------------------------------------------------------------------

def test_config_rhel_family_uses_conf_d_paths(mocker):
    """When detect_os_family returns rhel_family, config must set conf.d paths."""
    mocker.patch("vhost_helper.os_detector.detect_os_family", return_value="rhel_family")
    import importlib
    import vhost_helper.config as cfg
    importlib.reload(cfg)

    assert str(cfg.NGINX_SITES_AVAILABLE).endswith("conf.d") or "conf.d" in str(cfg.NGINX_SITES_AVAILABLE)
    assert cfg.NGINX_SITES_DISABLED is not None
    assert "conf.disabled" in str(cfg.NGINX_SITES_DISABLED)


def test_config_unknown_family_falls_back_to_debian_paths(mocker):
    """When detect_os_family returns 'unknown', config must use Debian-family defaults."""
    mocker.patch("vhost_helper.os_detector.detect_os_family", return_value="unknown")
    import importlib
    import vhost_helper.config as cfg
    importlib.reload(cfg)

    assert "sites-available" in str(cfg.NGINX_SITES_AVAILABLE)
    assert cfg.NGINX_SITES_DISABLED is None


# ---------------------------------------------------------------------------
# NginxProvider — RHEL-specific behaviour
# ---------------------------------------------------------------------------

@pytest.fixture
def rhel_provider(mocker, tmp_path):
    """NginxProvider patched for RHEL family with real tmp dirs."""
    conf_d = tmp_path / "conf.d"
    conf_disabled = tmp_path / "conf.disabled"
    conf_d.mkdir()
    conf_disabled.mkdir()

    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", conf_d)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", conf_d)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", conf_disabled)
    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "rhel_family")

    mock_run = mocker.patch("vhost_helper.providers.nginx.run_elevated_command",
                            return_value=subprocess.CompletedProcess([], 0))

    from vhost_helper.providers.nginx import NginxProvider
    provider = NginxProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")

    provider.conf_d = conf_d
    provider.conf_disabled = conf_disabled
    provider.mock_run = mock_run
    return provider


@pytest.fixture
def debian_provider(mocker, tmp_path):
    """NginxProvider patched for Debian family with real tmp dirs."""
    sites_available = tmp_path / "sites-available"
    sites_enabled = tmp_path / "sites-enabled"
    sites_available.mkdir()
    sites_enabled.mkdir()

    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_available)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "debian_family")

    mock_run = mocker.patch("vhost_helper.providers.nginx.run_elevated_command",
                            return_value=subprocess.CompletedProcess([], 0))

    from vhost_helper.providers.nginx import NginxProvider
    provider = NginxProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")

    provider.sites_available = sites_available
    provider.sites_enabled = sites_enabled
    provider.mock_run = mock_run
    return provider


class TestRhelDisableVhost:
    def test_disable_moves_file_from_conf_d_to_conf_disabled(self, rhel_provider):
        """disable_vhost on RHEL must issue an mv command, not rm."""
        (rhel_provider.conf_d / "qa015.local.conf").touch()
        rhel_provider.disable_vhost("qa015.local")
        calls = [c.args[0] for c in rhel_provider.mock_run.call_args_list]
        assert any("mv" in c for c in calls), "Expected mv command for RHEL disable"

    def test_disable_idempotent_when_already_disabled(self, rhel_provider):
        """disable_vhost on RHEL must not error when config is already absent from conf.d."""
        # File NOT in conf.d → already disabled; should be a no-op
        rhel_provider.disable_vhost("absent.local")
        rhel_provider.mock_run.assert_not_called()
        rhel_provider.reload.assert_not_called()

    def test_disable_creates_conf_disabled_dir_if_missing(self, rhel_provider, mocker):
        """disable_vhost must create conf.disabled directory if it doesn't exist."""
        (rhel_provider.conf_d / "qa015.local.conf").touch()

        # Simulate the directory not existing on first check but existing after mkdir
        original_exists = Path.exists

        def patched_exists(self):
            if "conf.disabled" in str(self) and not str(self).endswith("qa015.local.conf"):
                return False
            return original_exists(self)

        mocker.patch.object(Path, "exists", patched_exists)
        rhel_provider.disable_vhost("qa015.local")

        mkdir_calls = [c.args[0] for c in rhel_provider.mock_run.call_args_list]
        assert any("mkdir" in c for c in mkdir_calls), "Expected mkdir -p for conf.disabled"

    def test_disable_raises_when_disabled_path_not_configured(self, mocker, tmp_path):
        """disable_vhost must raise RuntimeError if NGINX_SITES_DISABLED is None on RHEL."""
        from vhost_helper.providers.nginx import NginxProvider
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", conf_d)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", conf_d)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)
        mocker.patch("vhost_helper.providers.nginx.detected_os_family", "rhel_family")
        mocker.patch("vhost_helper.providers.nginx.run_elevated_command")

        provider = NginxProvider()
        with pytest.raises(RuntimeError, match="NGINX_SITES_DISABLED path is not configured"):
            provider.disable_vhost("test.local")


class TestRhelEnableVhost:
    def test_enable_moves_file_from_conf_disabled_to_conf_d(self, rhel_provider):
        """enable_vhost on RHEL must issue an mv command."""
        (rhel_provider.conf_disabled / "qa015.local.conf").touch()
        with patch("pathlib.Path.exists", side_effect=[True, False]):
            rhel_provider.enable_vhost("qa015.local")
        calls = [c.args[0] for c in rhel_provider.mock_run.call_args_list]
        assert any("mv" in c for c in calls), "Expected mv command for RHEL enable"

    def test_enable_idempotent_when_already_enabled(self, rhel_provider):
        """enable_vhost on RHEL must not error when config is already in conf.d."""
        (rhel_provider.conf_d / "qa015.local.conf").touch()
        (rhel_provider.conf_disabled / "qa015.local.conf").touch()
        # Both exist → already enabled; should be a no-op
        with patch("pathlib.Path.exists", return_value=True):
            rhel_provider.enable_vhost("qa015.local")
        rhel_provider.mock_run.assert_not_called()
        rhel_provider.reload.assert_not_called()

    def test_enable_raises_file_not_found_when_config_missing(self, rhel_provider):
        """enable_vhost on RHEL must raise FileNotFoundError if config not in conf.disabled."""
        with patch("pathlib.Path.exists", return_value=False):
            with pytest.raises(FileNotFoundError):
                rhel_provider.enable_vhost("nonexistent.local")

    def test_enable_raises_when_disabled_path_not_configured(self, mocker, tmp_path):
        """enable_vhost must raise RuntimeError if NGINX_SITES_DISABLED is None on RHEL."""
        from vhost_helper.providers.nginx import NginxProvider
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", conf_d)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", conf_d)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)
        mocker.patch("vhost_helper.providers.nginx.detected_os_family", "rhel_family")
        mocker.patch("vhost_helper.providers.nginx.run_elevated_command")

        provider = NginxProvider()
        with pytest.raises(RuntimeError, match="NGINX_SITES_DISABLED path is not configured"):
            provider.enable_vhost("test.local")


class TestRemoveVhostPaths:
    def test_rhel_remove_checks_conf_disabled_too(self, rhel_provider):
        """remove_vhost on RHEL must include NGINX_SITES_DISABLED in search paths."""
        (rhel_provider.conf_disabled / "qa015.local.conf").touch()
        rhel_provider.remove_vhost("qa015.local", service_running=False)
        calls = [c.args[0] for c in rhel_provider.mock_run.call_args_list]
        # Should have issued an rm for the file in conf.disabled
        assert any("rm" in c for c in calls)

    def test_debian_remove_does_not_check_conf_disabled(self, debian_provider):
        """remove_vhost on Debian must NOT reference conf.disabled (it's None)."""
        (debian_provider.sites_available / "qa015.local.conf").touch()
        debian_provider.remove_vhost("qa015.local", service_running=False)
        calls = [c.args[0] for c in debian_provider.mock_run.call_args_list]
        assert not any("conf.disabled" in " ".join(c) for c in calls)


# ---------------------------------------------------------------------------
# main.py — enable/disable early-exit paths
# ---------------------------------------------------------------------------

class TestMainEnableDisableEarlyExit:
    def test_enable_exits_early_when_already_enabled(self, mocker, tmp_path):
        """enable command must print 'already enabled' and exit 0 if domain is active."""
        from typer.testing import CliRunner
        from vhost_helper.main import app

        available_dir = tmp_path / "sites-available"
        available_dir.mkdir()
        enabled_dir = tmp_path / "sites-enabled"
        enabled_dir.mkdir()
        (enabled_dir / "qa015.local.conf").touch()

        # Mock Nginx paths
        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available_dir)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled_dir)
        # Mock Apache paths to empty temp dirs to avoid host interference
        apache_available = tmp_path / "apache-available"
        apache_available.mkdir()
        apache_enabled = tmp_path / "apache-enabled"
        apache_enabled.mkdir()
        mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", apache_available)
        mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", apache_enabled)
        mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

        runner = CliRunner()
        result = runner.invoke(app, ["enable", "qa015.local"])
        assert result.exit_code == 0
        assert "already enabled" in result.output

    def test_disable_exits_early_when_already_disabled(self, mocker, tmp_path):
        """disable command must print 'already disabled' and exit 0 if domain is inactive."""
        from typer.testing import CliRunner
        from vhost_helper.main import app

        available_dir = tmp_path / "sites-available"
        available_dir.mkdir()
        enabled_dir = tmp_path / "sites-enabled"
        enabled_dir.mkdir()
        # To be detected as Nginx, the file should exist in sites-available
        (available_dir / "qa015.local.conf").touch()
        # No file in sites-enabled → domain is already disabled

        # Mock Nginx paths
        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available_dir)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled_dir)
        # Mock Apache paths to empty temp dirs
        apache_available = tmp_path / "apache-available"
        apache_available.mkdir()
        apache_enabled = tmp_path / "apache-enabled"
        apache_enabled.mkdir()
        mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", apache_available)
        mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", apache_enabled)
        mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

        runner = CliRunner()
        result = runner.invoke(app, ["disable", "qa015.local"])
        assert result.exit_code == 0
        assert "already disabled" in result.output

    def test_enable_validates_domain_format(self, mocker):
        """enable command must reject malformed domain names (bad args → non-zero exit)."""
        from typer.testing import CliRunner
        from vhost_helper.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["enable", "bad..domain"])
        assert result.exit_code != 0

    def test_disable_validates_domain_format(self, mocker):
        """disable command must reject malformed domain names."""
        from typer.testing import CliRunner
        from vhost_helper.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["disable", "bad..domain"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# README documentation — acceptance criteria verification
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

@pytest.mark.parametrize("readme_path", [
    _PROJECT_ROOT / "README.md",
    _PROJECT_ROOT / "src" / "README.md",
])
def test_readme_contains_manual_installation_section(readme_path):
    """Both README files must contain a 'Manual Installation' section (AC Feature 4)."""
    assert readme_path.exists(), f"README not found at {readme_path}"
    content = readme_path.read_text()
    assert "Manual Installation" in content, (
        f"'Manual Installation' section missing from {readme_path}"
    )


@pytest.mark.parametrize("readme_path", [
    _PROJECT_ROOT / "README.md",
    _PROJECT_ROOT / "src" / "README.md",
])
def test_readme_manual_installation_has_five_steps(readme_path):
    """Manual Installation section should document cloning, venv, deps, symlink, completion."""
    content = readme_path.read_text()
    for keyword in ["git clone", "venv", "requirements.txt", "ln -s", "bash_completion"]:
        assert keyword in content, (
            f"Manual Installation in {readme_path.name} is missing step for: {keyword!r}"
        )


@pytest.mark.parametrize("readme_path", [
    _PROJECT_ROOT / "README.md",
    _PROJECT_ROOT / "src" / "README.md",
])
def test_readme_contains_server_support_clarification(readme_path):
    """Both README files must state supported web servers (Nginx or Apache)."""
    content = readme_path.read_text()
    assert "Nginx" in content and "Apache" in content, (
        f"Server support clarification missing from {readme_path}"
    )


@pytest.mark.parametrize("readme_path", [
    _PROJECT_ROOT / "README.md",
    _PROJECT_ROOT / "src" / "README.md",
])
def test_readme_references_install_sh(readme_path):
    """Manual Installation section must cross-reference install.sh (AC Feature 4)."""
    content = readme_path.read_text()
    assert "install.sh" in content, (
        f"install.sh not referenced in {readme_path.name}"
    )


# ---------------------------------------------------------------------------
# SELinux error message — verifies BUG-001 fix
# ---------------------------------------------------------------------------

def test_selinux_error_message_includes_chcon_command(mocker, vhost_config):
    """Failure message after chcon must include the manual chcon command (BUG-001 fix)."""
    import subprocess as sp
    from vhost_helper.providers.nginx import NginxProvider

    tmp = Path(vhost_config.document_root).parent
    sites_avail = tmp / "sites-available"
    sites_avail.mkdir(exist_ok=True)
    sites_enabled = tmp / "sites-enabled"
    sites_enabled.mkdir(exist_ok=True)

    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_avail)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.nginx.is_selinux_enforcing", return_value=True)

    def fail_on_chcon(cmd, **kwargs):
        if "chcon" in cmd:
            raise RuntimeError("permission denied")
        return sp.CompletedProcess(cmd, 0)

    mocker.patch("vhost_helper.providers.nginx.run_elevated_command", side_effect=fail_on_chcon)

    provider = NginxProvider()
    mocker.patch.object(provider, "remove_vhost")

    with pytest.raises(RuntimeError) as exc_info:
        provider.create_vhost(vhost_config)

    msg = str(exc_info.value)
    assert "sudo chcon -t httpd_config_t" in msg, (
        f"Error must include manual chcon command; got: {msg!r}"
    )
    assert "Failed to apply SELinux context" in msg
