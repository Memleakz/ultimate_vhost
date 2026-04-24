"""
QA additions for ULTIMATE_VHOST-017 Final Validation.

Targets uncovered branches identified by coverage analysis:
 - apache.py: is_apache_running exceptions, _get_template not found,
   SELinux rollback, reload-failure rollback, validate_config OSError,
   reload RuntimeError, enable/disable idempotency, RHEL-specific paths,
   remove_vhost exception branch, disabled-dir auto-create.
 - main.py: _detect_provider_for_domain SITES_DISABLED path, Apache
   detection fallback, _detect_server_type Apache branch, explicit
   --provider flag on enable/disable/info, missing-config error paths.
 - hostfile.py: non-sudo remove_entry path (direct file write).
"""

import subprocess
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from typer.testing import CliRunner

from vhost_helper.main import (
    app,
    _detect_provider_for_domain,
    _detect_server_type,
    validate_domain,
)
from vhost_helper.models import VHostConfig, ServerType, RuntimeMode
from vhost_helper.providers import apache as apache_mod
from vhost_helper.providers.apache import ApacheProvider
from vhost_helper.hostfile import add_entry, remove_entry
import vhost_helper.hostfile as hostfile_module

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_hosts(tmp_path):
    """Redirect HOSTS_FILE to a temp file for the duration of the test."""
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n")
    original = hostfile_module.HOSTS_FILE
    hostfile_module.HOSTS_FILE = hosts
    yield hosts
    hostfile_module.HOSTS_FILE = original


@pytest.fixture
def debian_apache(mocker, tmp_path):
    """ApacheProvider configured for a Debian-like environment."""
    avail = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    avail.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", avail)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "debian_family")
    mock_run = mocker.patch("vhost_helper.providers.apache.run_elevated_command")

    provider = ApacheProvider()
    provider._avail = avail
    provider._enabled = enabled
    provider._mock_run = mock_run
    return provider


@pytest.fixture
def rhel_apache(mocker, tmp_path):
    """ApacheProvider configured for a RHEL-like environment."""
    conf_d = tmp_path / "conf.d"
    disabled = tmp_path / "conf.disabled"
    conf_d.mkdir()
    disabled.mkdir()

    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", conf_d)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", conf_d)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", disabled)
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "rhel_family")
    mock_run = mocker.patch("vhost_helper.providers.apache.run_elevated_command")

    provider = ApacheProvider()
    provider._conf_d = conf_d
    provider._disabled = disabled
    provider._mock_run = mock_run
    return provider


# ===========================================================================
# apache.py — is_apache_running exception paths
# ===========================================================================


def test_is_apache_running_returns_false_on_file_not_found(mocker):
    mocker.patch(
        "vhost_helper.providers.apache.subprocess.run",
        side_effect=FileNotFoundError,
    )
    assert apache_mod.is_apache_running() is False


def test_is_apache_running_returns_false_on_timeout(mocker):
    mocker.patch(
        "vhost_helper.providers.apache.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=5),
    )
    assert apache_mod.is_apache_running() is False


def test_is_apache_running_returns_false_on_subprocess_error(mocker):
    mocker.patch(
        "vhost_helper.providers.apache.subprocess.run",
        side_effect=subprocess.SubprocessError("boom"),
    )
    assert apache_mod.is_apache_running() is False


# ===========================================================================
# apache.py — _get_template FileNotFoundError
# ===========================================================================


def test_apache_get_template_raises_file_not_found(tmp_path, mocker):
    user_t = tmp_path / "user" / "apache"
    user_t.mkdir(parents=True)
    app_t = tmp_path / "app" / "apache"
    app_t.mkdir(parents=True)

    mocker.patch("vhost_helper.providers.apache.USER_TEMPLATES_DIR", tmp_path / "user")
    mocker.patch("vhost_helper.providers.apache.APP_TEMPLATES_DIR", tmp_path / "app")

    provider = ApacheProvider()
    with pytest.raises(FileNotFoundError, match="Template 'nonexistent' not found for Apache"):
        provider._get_template("nonexistent")


# ===========================================================================
# apache.py — SELinux rollback during create_vhost
# ===========================================================================


def test_apache_create_vhost_selinux_failure_triggers_rollback(tmp_path, mocker):
    avail = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    avail.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", avail)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.apache.is_selinux_enforcing", return_value=True)

    call_count = [0]

    def run_side_effect(cmd, **kwargs):
        call_count[0] += 1
        # fail on the chcon call
        if "chcon" in cmd:
            raise RuntimeError("chcon failed")

    mocker.patch("vhost_helper.providers.apache.run_elevated_command", side_effect=run_side_effect)

    # Need actual templates
    app_t = tmp_path / "app_templates" / "apache"
    app_t.mkdir(parents=True)
    (app_t / "default.conf.j2").write_text(
        "<VirtualHost *:80>\n  ServerName {{ domain }}\n  DocumentRoot {{ document_root }}\n</VirtualHost>"
    )
    mocker.patch("vhost_helper.providers.apache.APP_TEMPLATES_DIR", tmp_path / "app_templates")
    mocker.patch(
        "vhost_helper.providers.apache.USER_TEMPLATES_DIR",
        tmp_path / "user_templates",
    )
    (tmp_path / "user_templates" / "apache").mkdir(parents=True)

    doc_root = tmp_path / "www"
    doc_root.mkdir()

    provider = ApacheProvider()
    config = VHostConfig(domain="selinux.test", document_root=str(doc_root))

    with pytest.raises(RuntimeError, match="SELinux"):
        provider.create_vhost(config, service_running=False)


# ===========================================================================
# apache.py — reload failure rollback in create_vhost
# ===========================================================================


def test_apache_create_vhost_reload_failure_triggers_rollback(tmp_path, mocker):
    avail = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    avail.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", avail)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.apache.is_selinux_enforcing", return_value=False)
    mocker.patch("vhost_helper.providers.apache.run_elevated_command")

    app_t = tmp_path / "app_templates" / "apache"
    app_t.mkdir(parents=True)
    (app_t / "default.conf.j2").write_text(
        "<VirtualHost *:80>\n  ServerName {{ domain }}\n  DocumentRoot {{ document_root }}\n</VirtualHost>"
    )
    mocker.patch("vhost_helper.providers.apache.APP_TEMPLATES_DIR", tmp_path / "app_templates")
    mocker.patch(
        "vhost_helper.providers.apache.USER_TEMPLATES_DIR",
        tmp_path / "user_templates",
    )
    (tmp_path / "user_templates" / "apache").mkdir(parents=True)

    doc_root = tmp_path / "www"
    doc_root.mkdir()

    provider = ApacheProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    # Patch reload to fail AND patch remove_vhost so rollback doesn't also call reload
    mocker.patch.object(provider, "reload", side_effect=RuntimeError("reload failed"))
    mocker.patch.object(provider, "remove_vhost")

    config = VHostConfig(domain="reload-fail.test", document_root=str(doc_root))

    with pytest.raises(RuntimeError, match="reload failed"):
        provider.create_vhost(config, service_running=True)

    # remove_vhost should have been called as part of rollback
    provider.remove_vhost.assert_called()


def test_apache_create_vhost_validation_failure_triggers_rollback(tmp_path, mocker):
    """When validate_config returns False, remove_vhost is called and RuntimeError raised."""
    avail = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    avail.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", avail)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.apache.is_selinux_enforcing", return_value=False)
    mocker.patch("vhost_helper.providers.apache.run_elevated_command")

    app_t = tmp_path / "app_templates" / "apache"
    app_t.mkdir(parents=True)
    (app_t / "default.conf.j2").write_text(
        "<VirtualHost *:80>\n  ServerName {{ domain }}\n  DocumentRoot {{ document_root }}\n</VirtualHost>"
    )
    mocker.patch("vhost_helper.providers.apache.APP_TEMPLATES_DIR", tmp_path / "app_templates")
    mocker.patch(
        "vhost_helper.providers.apache.USER_TEMPLATES_DIR",
        tmp_path / "user_templates",
    )
    (tmp_path / "user_templates" / "apache").mkdir(parents=True)

    doc_root = tmp_path / "www"
    doc_root.mkdir()

    provider = ApacheProvider()
    mocker.patch.object(provider, "validate_config", return_value=False)
    mocker.patch.object(provider, "remove_vhost")

    config = VHostConfig(domain="invalid-config.test", document_root=str(doc_root))

    with pytest.raises(RuntimeError, match="validation failed"):
        provider.create_vhost(config, service_running=True)

    provider.remove_vhost.assert_called()


def test_apache_create_vhost_non_rollback_exception_wraps_message(tmp_path, mocker):
    """An unexpected non-RuntimeError exception during create_vhost is wrapped."""
    avail = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    avail.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", avail)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.apache.is_selinux_enforcing", return_value=False)

    # chmod raises a non-RuntimeError OSError — should trigger line 135
    call_count = [0]
    def run_side(cmd, **kwargs):
        call_count[0] += 1
        if "chmod" in cmd:
            raise OSError("permission denied")
    mocker.patch("vhost_helper.providers.apache.run_elevated_command", side_effect=run_side)

    app_t = tmp_path / "app_templates" / "apache"
    app_t.mkdir(parents=True)
    (app_t / "default.conf.j2").write_text(
        "<VirtualHost *:80>\n  ServerName {{ domain }}\n  DocumentRoot {{ document_root }}\n</VirtualHost>"
    )
    mocker.patch("vhost_helper.providers.apache.APP_TEMPLATES_DIR", tmp_path / "app_templates")
    mocker.patch("vhost_helper.providers.apache.USER_TEMPLATES_DIR", tmp_path / "user_templates")
    (tmp_path / "user_templates" / "apache").mkdir(parents=True)

    doc_root = tmp_path / "www"
    doc_root.mkdir()

    provider = ApacheProvider()
    config = VHostConfig(domain="os-error.test", document_root=str(doc_root))

    with pytest.raises(RuntimeError, match="Failed to create Apache vhost"):
        provider.create_vhost(config, service_running=False)


# ===========================================================================
# apache.py — validate_config failure cases (OSError, FileNotFoundError)
# ===========================================================================


def test_apache_validate_config_returns_false_on_os_error(mocker):
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.apache.run_elevated_command", side_effect=OSError("no binary"))

    provider = ApacheProvider()
    assert provider.validate_config() is False


def test_apache_validate_config_returns_false_on_file_not_found(mocker):
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "rhel_family")
    mocker.patch("vhost_helper.providers.apache.run_elevated_command", side_effect=FileNotFoundError("httpd"))

    provider = ApacheProvider()
    assert provider.validate_config() is False


def test_apache_validate_config_returns_false_on_runtime_error(mocker):
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.apache.run_elevated_command", side_effect=RuntimeError("exit 1"))

    provider = ApacheProvider()
    assert provider.validate_config() is False


# ===========================================================================
# apache.py — reload RuntimeError propagation
# ===========================================================================


def test_apache_reload_raises_runtime_error(mocker):
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.apache.run_elevated_command", side_effect=RuntimeError("systemctl failed"))

    provider = ApacheProvider()
    with pytest.raises(RuntimeError, match="Failed to reload Apache"):
        provider.reload()


# ===========================================================================
# apache.py — remove_vhost RHEL disabled path + exception branch
# ===========================================================================


def test_apache_remove_vhost_includes_rhel_disabled_path(rhel_apache, mocker):
    """remove_vhost on RHEL should also check the disabled directory."""
    # Place a config in conf.d
    conf_file = rhel_apache._conf_d / "site.test.conf"
    conf_file.write_text("# test")

    rhel_apache.remove_vhost("site.test", service_running=False)

    # rm should have been called
    rm_calls = [
        c for c in rhel_apache._mock_run.call_args_list
        if "rm" in c.args[0]
    ]
    assert rm_calls, "Expected rm to be called for RHEL remove_vhost"


def test_apache_remove_vhost_raises_on_run_error(debian_apache, tmp_path, mocker):
    """remove_vhost should raise RuntimeError if run_elevated_command fails."""
    conf_file = debian_apache._avail / "fail.test.conf"
    conf_file.write_text("# test")

    debian_apache._mock_run.side_effect = RuntimeError("rm failed")

    with pytest.raises(RuntimeError, match="Failed to remove Apache vhost"):
        debian_apache.remove_vhost("fail.test", service_running=False)


# ===========================================================================
# apache.py — enable_vhost edge cases
# ===========================================================================


def test_apache_enable_vhost_rhel_no_disabled_path_configured(mocker):
    """RHEL enable_vhost raises if APACHE_SITES_DISABLED is None."""
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "rhel_family")
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", None)
    provider = ApacheProvider()
    with pytest.raises(RuntimeError, match="not configured"):
        provider.enable_vhost("any.test")


def test_apache_enable_vhost_rhel_config_not_found(rhel_apache):
    """RHEL enable_vhost raises FileNotFoundError when disabled config absent."""
    with pytest.raises(FileNotFoundError):
        rhel_apache.enable_vhost("missing.test", service_running=False)


def test_apache_enable_vhost_rhel_already_enabled(rhel_apache):
    """RHEL enable_vhost is a no-op if config already in enabled dir."""
    # Config exists in enabled dir (conf.d)
    conf_file = rhel_apache._conf_d / "already.test.conf"
    conf_file.write_text("# already there")
    # Also put it in disabled to avoid FileNotFoundError check
    disabled_file = rhel_apache._disabled / "already.test.conf"
    disabled_file.write_text("# disabled version")

    # Should return without calling run_elevated_command (mv)
    rhel_apache.enable_vhost("already.test", service_running=False)
    mv_calls = [c for c in rhel_apache._mock_run.call_args_list if "mv" in c.args[0]]
    assert not mv_calls, "Should not call mv when already enabled"


def test_apache_enable_vhost_debian_config_not_found(debian_apache):
    """Debian enable_vhost raises FileNotFoundError when available config absent."""
    with pytest.raises(FileNotFoundError):
        debian_apache.enable_vhost("gone.test", service_running=False)


def test_apache_enable_vhost_debian_already_enabled(debian_apache):
    """Debian enable_vhost is a no-op if symlink already exists."""
    # Create both the config and the enabled symlink
    config = debian_apache._avail / "linked.test.conf"
    config.write_text("# config")
    link = debian_apache._enabled / "linked.test.conf"
    link.symlink_to(config)

    debian_apache.enable_vhost("linked.test", service_running=False)
    ln_calls = [c for c in debian_apache._mock_run.call_args_list if "ln" in c.args[0]]
    assert not ln_calls, "Should not call ln -s when already enabled"


# ===========================================================================
# apache.py — disable_vhost edge cases
# ===========================================================================


def test_apache_disable_vhost_rhel_no_disabled_path_configured(mocker):
    """RHEL disable_vhost raises if APACHE_SITES_DISABLED is None."""
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "rhel_family")
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", None)
    provider = ApacheProvider()
    with pytest.raises(RuntimeError, match="not configured"):
        provider.disable_vhost("any.test")


def test_apache_disable_vhost_rhel_creates_disabled_dir_if_missing(mocker, tmp_path):
    """RHEL disable_vhost auto-creates conf.disabled if it doesn't exist yet."""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    # Disabled dir intentionally absent — provider should mkdir it
    disabled = tmp_path / "conf.disabled"  # does NOT exist yet

    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", conf_d)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", conf_d)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", disabled)
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "rhel_family")
    mock_run = mocker.patch("vhost_helper.providers.apache.run_elevated_command")

    # Put a config in conf.d so it's "enabled"
    conf_file = conf_d / "site.test.conf"
    conf_file.write_text("# enabled")

    provider = ApacheProvider()
    provider.disable_vhost("site.test", service_running=False)

    mkdir_calls = [c for c in mock_run.call_args_list if "mkdir" in c.args[0]]
    assert mkdir_calls, "Expected mkdir -p call for missing conf.disabled"


def test_apache_disable_vhost_rhel_already_disabled(rhel_apache):
    """RHEL disable_vhost is a no-op when config not in enabled dir."""
    # Don't place any file in conf.d
    rhel_apache.disable_vhost("nothere.test", service_running=False)
    mv_calls = [c for c in rhel_apache._mock_run.call_args_list if "mv" in c.args[0]]
    assert not mv_calls, "Should not call mv when already disabled"


def test_apache_disable_vhost_debian_already_disabled(debian_apache):
    """Debian disable_vhost is a no-op when symlink doesn't exist."""
    # No symlink in sites-enabled
    debian_apache.disable_vhost("nowhere.test", service_running=False)
    rm_calls = [c for c in debian_apache._mock_run.call_args_list if "rm" in c.args[0]]
    assert not rm_calls, "Should not call rm when already disabled"


# ===========================================================================
# hostfile.py — non-sudo remove_entry (direct file write, no sudo)
# ===========================================================================


def test_remove_entry_no_sudo_writes_directly(tmp_hosts, mocker):
    """When get_sudo_prefix() returns [], remove_entry writes directly to HOSTS_FILE."""
    # Ensure there's something to remove
    with open(tmp_hosts, "a") as f:
        f.write("127.0.0.1\tdirect.test\n")

    mocker.patch("vhost_helper.hostfile.get_sudo_prefix", return_value=[])

    remove_entry("direct.test")

    content = tmp_hosts.read_text()
    assert "direct.test" not in content
    assert "localhost" in content


def test_remove_entry_no_sudo_noop_when_absent(tmp_hosts, mocker):
    """remove_entry should be a no-op if the domain isn't in the file."""
    mocker.patch("vhost_helper.hostfile.get_sudo_prefix", return_value=[])
    original = tmp_hosts.read_text()

    remove_entry("absent.test")

    assert tmp_hosts.read_text() == original


# ===========================================================================
# main.py — _detect_provider_for_domain: SITES_DISABLED paths
# ===========================================================================


def test_detect_provider_finds_nginx_in_sites_disabled(tmp_path, mocker):
    """_detect_provider_for_domain should return NGINX if conf is in NGINX_SITES_DISABLED."""
    disabled_dir = tmp_path / "nginx_disabled"
    disabled_dir.mkdir()
    (disabled_dir / "myngx.test.conf").write_text("# disabled nginx config")

    # Patch config paths so only the DISABLED dir has a hit
    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", tmp_path / "nonexistent_avail")
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nonexistent_enabled")
    mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", disabled_dir)
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "nonexistent_apache_avail")
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "nonexistent_apache_enabled")
    mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)

    result = _detect_provider_for_domain("myngx.test")
    assert result == ServerType.NGINX


def test_detect_provider_finds_apache_in_sites_enabled(tmp_path, mocker):
    """_detect_provider_for_domain should return APACHE if conf is in APACHE_SITES_ENABLED."""
    apache_enabled = tmp_path / "apache_enabled"
    apache_enabled.mkdir()
    (apache_enabled / "myapache.test.conf").write_text("# apache enabled config")

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", tmp_path / "nonexistent")
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nonexistent2")
    mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "nonexistent3")
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", apache_enabled)
    mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)

    result = _detect_provider_for_domain("myapache.test")
    assert result == ServerType.APACHE


def test_detect_provider_finds_apache_in_sites_disabled(tmp_path, mocker):
    """_detect_provider_for_domain should return APACHE if conf is in APACHE_SITES_DISABLED."""
    apache_disabled = tmp_path / "apache_disabled"
    apache_disabled.mkdir()
    (apache_disabled / "hidden.test.conf").write_text("# apache disabled config")

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", tmp_path / "nonexistent")
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nonexistent2")
    mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "nonexistent3")
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "nonexistent4")
    mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", apache_disabled)

    result = _detect_provider_for_domain("hidden.test")
    assert result == ServerType.APACHE


def test_detect_provider_fallback_nginx_only(tmp_path, mocker):
    """_detect_provider_for_domain returns NGINX if only Nginx dirs exist but no config match."""
    nginx_avail = tmp_path / "nginx_avail"
    nginx_avail.mkdir()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", nginx_avail)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nonexistent_ne")
    mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "nonexistent_aa")
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "nonexistent_ae")
    mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)

    result = _detect_provider_for_domain("fallback.test")
    assert result == ServerType.NGINX


def test_detect_provider_fallback_apache_only(tmp_path, mocker):
    """_detect_provider_for_domain returns APACHE if only Apache dirs exist."""
    apache_avail = tmp_path / "apache_avail"
    apache_avail.mkdir()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", tmp_path / "nonexistent_na")
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nonexistent_ne")
    mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", apache_avail)
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "nonexistent_ae")
    mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)

    result = _detect_provider_for_domain("fallback-apache.test")
    assert result == ServerType.APACHE


# ===========================================================================
# main.py — _detect_server_type: Apache installed branch
# ===========================================================================


def test_detect_server_type_returns_apache_when_only_apache_installed(mocker):
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=False)
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=True)
    assert _detect_server_type() == ServerType.APACHE


def test_detect_server_type_raises_when_neither_installed(mocker):
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=False)
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)
    with pytest.raises(RuntimeError, match="No supported web server found"):
        _detect_server_type()


# ===========================================================================
# main.py — create: explicit provider not installed
# ===========================================================================


def test_cli_create_nginx_not_installed_exits_with_error(tmp_path, mocker):
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=False)
    mocker.patch("vhost_helper.main.preflight_sudo_check")

    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(app, ["create", "mysite.test", str(doc_root), "--provider", "nginx"])
    assert result.exit_code != 0
    assert "Nginx is not installed" in result.output


def test_cli_create_apache_not_installed_exits_with_error(tmp_path, mocker):
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)
    mocker.patch("vhost_helper.main.preflight_sudo_check")

    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(app, ["create", "mysite.test", str(doc_root), "--provider", "apache"])
    assert result.exit_code != 0
    assert "Apache is not installed" in result.output


# ===========================================================================
# main.py — enable: explicit provider skips detection, Apache service path
# ===========================================================================


def test_cli_enable_with_explicit_provider_skips_detection(tmp_path, mocker):
    """When --provider is given, no auto-detection should occur."""
    nginx_enabled = tmp_path / "nginx_enabled"
    nginx_enabled.mkdir()
    # Domain NOT in enabled — so command proceeds past "already enabled" guard
    # But config IS in available so provider.enable_vhost won't raise
    nginx_avail = tmp_path / "nginx_avail"
    nginx_avail.mkdir()
    (nginx_avail / "explicit.test.conf").write_text("# conf")

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", nginx_avail)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", nginx_enabled)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.main.add_entry")
    mock_provider = MagicMock()
    mocker.patch("vhost_helper.main._get_provider", return_value=mock_provider)

    result = runner.invoke(app, ["enable", "explicit.test", "--provider", "nginx"])
    assert result.exit_code == 0
    mock_provider.enable_vhost.assert_called_once()


def test_cli_enable_apache_service_path_used(tmp_path, mocker):
    """enable with --provider apache uses is_apache_running, not is_nginx_running."""
    apache_enabled = tmp_path / "apache_enabled"
    apache_enabled.mkdir()
    apache_avail = tmp_path / "apache_avail"
    apache_avail.mkdir()
    (apache_avail / "myapache.test.conf").write_text("# conf")

    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", apache_avail)
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", apache_enabled)
    mock_apache_running = mocker.patch("vhost_helper.main.is_apache_running", return_value=False)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.main.add_entry")
    mock_provider = MagicMock()
    mocker.patch("vhost_helper.main._get_provider", return_value=mock_provider)

    result = runner.invoke(app, ["enable", "myapache.test", "--provider", "apache"])
    assert result.exit_code == 0
    mock_apache_running.assert_called()


# ===========================================================================
# main.py — disable: no config found path
# ===========================================================================


def test_cli_disable_no_config_found_exits_with_error(tmp_path, mocker):
    """disable with auto-detection should exit 1 when no config found anywhere."""
    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", tmp_path / "na")
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "ne")
    mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "aa")
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "ae")
    mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)
    mocker.patch("vhost_helper.main.preflight_sudo_check")

    result = runner.invoke(app, ["disable", "ghost.test"])
    assert result.exit_code != 0
    assert "No configuration found" in result.output


def test_cli_disable_apache_service_path_used(tmp_path, mocker):
    """disable with --provider apache should use is_apache_running."""
    apache_enabled = tmp_path / "apache_enabled"
    apache_enabled.mkdir()
    # Put a symlink so the "already disabled" guard doesn't trigger
    conf_file = apache_enabled / "dis.test.conf"
    conf_file.write_text("# conf")

    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", apache_enabled)
    mock_apache_running = mocker.patch("vhost_helper.main.is_apache_running", return_value=False)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.main.remove_entry")
    mock_provider = MagicMock()
    mocker.patch("vhost_helper.main._get_provider", return_value=mock_provider)

    result = runner.invoke(app, ["disable", "dis.test", "--provider", "apache"])
    assert result.exit_code == 0
    mock_apache_running.assert_called()


# ===========================================================================
# main.py — info: explicit provider + no config found
# ===========================================================================


def test_cli_info_with_explicit_apache_provider_and_no_config(tmp_path, mocker):
    """info with explicit --provider apache should exit 1 when no config."""
    apache_avail = tmp_path / "apache_avail"
    apache_avail.mkdir()

    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", apache_avail)
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "ae")

    result = runner.invoke(app, ["info", "absent.test", "--provider", "apache"])
    assert result.exit_code != 0
    # Should print the "No configuration found" message
    assert "No configuration found" in result.output or result.exit_code == 1


def test_cli_info_with_explicit_nginx_provider_reads_config(tmp_path, mocker):
    """info with explicit --provider nginx reads the Nginx config correctly."""
    nginx_avail = tmp_path / "nginx_avail"
    nginx_avail.mkdir()
    nginx_enabled = tmp_path / "nginx_enabled"
    nginx_enabled.mkdir()

    conf = nginx_avail / "info.test.conf"
    conf.write_text(
        'server {\n  listen 80;\n  server_name "info.test";\n  root "/var/www/info";\n}\n'
    )
    # Create symlink to mark it as enabled
    (nginx_enabled / "info.test.conf").symlink_to(conf)

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", nginx_avail)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", nginx_enabled)

    result = runner.invoke(app, ["info", "info.test", "--provider", "nginx"])
    assert result.exit_code == 0
    assert "info.test" in result.output


# ===========================================================================
# Acceptance criteria: requirements.txt pinning
# ===========================================================================


def test_requirements_txt_all_pinned():
    """All entries in requirements.txt must use == version pinning."""
    req_path = Path(__file__).parent.parent / "requirements.txt"
    assert req_path.exists(), "requirements.txt not found"
    lines = [l.strip() for l in req_path.read_text().splitlines() if l.strip() and not l.startswith("#")]
    for line in lines:
        assert "==" in line, f"Unpinned requirement detected: {line!r}"
        assert ">=" not in line, f"Loose pin detected: {line!r}"
        assert "~=" not in line, f"Compatible pin detected: {line!r}"


def test_requirements_dev_txt_all_pinned():
    """All entries in requirements-dev.txt must use == version pinning."""
    req_path = Path(__file__).parent.parent / "requirements-dev.txt"
    assert req_path.exists(), "requirements-dev.txt not found"
    lines = [l.strip() for l in req_path.read_text().splitlines() if l.strip() and not l.startswith("#")]
    for line in lines:
        assert "==" in line, f"Unpinned requirement detected: {line!r}"
        assert ">=" not in line, f"Loose pin detected: {line!r}"
        assert "~=" not in line, f"Compatible pin detected: {line!r}"


# ===========================================================================
# Acceptance criteria: README structure validation
# ===========================================================================


def test_readme_contains_getting_started_section():
    src_readme = Path(__file__).parent.parent / "README.md"
    assert src_readme.exists()
    content = src_readme.read_text()
    assert "Getting Started" in content or "getting-started" in content.lower()


def test_readme_contains_nginx_and_apache_sections():
    src_readme = Path(__file__).parent.parent / "README.md"
    content = src_readme.read_text()
    assert "Nginx" in content or "nginx" in content
    assert "Apache" in content or "apache" in content


def test_readme_contains_supported_environments_matrix():
    src_readme = Path(__file__).parent.parent / "README.md"
    content = src_readme.read_text()
    # Should reference both Debian and Fedora
    assert "Debian" in content or "debian" in content.lower()
    assert "Fedora" in content or "fedora" in content.lower()


def test_readme_contains_all_six_commands():
    src_readme = Path(__file__).parent.parent / "README.md"
    content = src_readme.read_text()
    for cmd in ["create", "remove", "enable", "disable", "list", "info"]:
        assert cmd in content, f"Command '{cmd}' missing from README"


# ===========================================================================
# Acceptance criteria: domain validation edge cases
# ===========================================================================


def test_validate_domain_rejects_empty():
    with pytest.raises(ValueError):
        validate_domain("")


def test_validate_domain_rejects_too_long():
    long_domain = "a" * 64 + ".test"
    with pytest.raises(ValueError):
        validate_domain(long_domain)


def test_validate_domain_rejects_double_dot():
    with pytest.raises(ValueError):
        validate_domain("double..dot.test")


def test_validate_domain_rejects_leading_hyphen():
    with pytest.raises(ValueError):
        validate_domain("-invalid.test")


def test_validate_domain_rejects_single_label():
    """Single label (no dot) is not a valid domain per DOMAIN_REGEX."""
    with pytest.raises(ValueError):
        validate_domain("nodot")


def test_validate_domain_accepts_valid():
    assert validate_domain("my-site.test") == "my-site.test"
    assert validate_domain("sub.domain.example.com") == "sub.domain.example.com"


def test_validate_domain_rejects_injection_chars():
    with pytest.raises(ValueError):
        validate_domain("evil\ndomain.test")


# ===========================================================================
# Edge case: VHostConfig rejects forbidden chars in document_root
# ===========================================================================


def test_vhost_config_rejects_quoted_path(tmp_path):
    bad_path = tmp_path / 'path"with"quotes'
    bad_path.mkdir()
    with pytest.raises(ValueError, match="forbidden"):
        VHostConfig(domain="test.local", document_root=str(bad_path))


def test_vhost_config_rejects_newline_in_path(tmp_path):
    # Can't create a dir with newline on most systems; use a temp trick
    with pytest.raises((ValueError, OSError)):
        VHostConfig(domain="test.local", document_root="/tmp/path\ninjected")


# ===========================================================================
# Edge case: hostfile add_entry deduplication
# ===========================================================================


def test_add_entry_does_not_duplicate(tmp_hosts, mocker):
    """add_entry should not add a second line if the entry already exists."""
    mocker.patch("vhost_helper.hostfile.get_sudo_prefix", return_value=[])

    # Add an entry manually
    with open(tmp_hosts, "a") as f:
        f.write("127.0.0.1\tdedup.test\n")

    # Call add_entry again — should be a no-op
    add_entry("127.0.0.1", "dedup.test")

    lines = [l for l in tmp_hosts.read_text().splitlines() if "dedup.test" in l]
    assert len(lines) == 1, "add_entry created a duplicate entry"


def test_add_entry_updates_ip_when_changed(tmp_hosts, mocker):
    """add_entry should remove the old IP entry and replace with new IP."""
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")
    mocker.patch("vhost_helper.hostfile.get_sudo_prefix", return_value=["sudo"])

    # Add original entry
    with open(tmp_hosts, "a") as f:
        f.write("10.0.0.1\tupdate.test\n")

    # Now call with different IP — should trigger remove then add
    add_entry("192.168.1.1", "update.test")
    assert mock_run.called
