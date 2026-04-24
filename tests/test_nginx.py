import pytest
import subprocess
from pathlib import Path
from unittest.mock import patch

from vhost_helper.models import VHostConfig, ServerType, RuntimeMode
from vhost_helper.providers import nginx

@pytest.fixture
def vhost_config(tmp_path):
    """Provides a basic VHostConfig for tests."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    return VHostConfig(
        domain="test.local",
        document_root=str(doc_root),
        port=80,
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.STATIC
    )

@pytest.fixture
def patched_nginx_provider(mocker, tmp_path):
    """
    Provides an NginxProvider instance with patched dependencies for Debian.
    Patches nginx module-level variables directly to avoid importlib.reload(),
    which would create a new class object and break cross-test mock targeting.
    """
    sites_available = tmp_path / "sites-available"
    sites_enabled = tmp_path / "sites-enabled"
    sites_available.mkdir()
    sites_enabled.mkdir()

    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_available)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False)

    mock_run = mocker.patch("vhost_helper.providers.nginx.run_elevated_command")

    provider = nginx.NginxProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")

    provider.mock_run = mock_run
    return provider

@pytest.fixture
def rhel_patched_nginx_provider(mocker, tmp_path):
    """
    Provides an NginxProvider instance patched for a RHEL-like environment.
    Patches nginx module-level variables directly to avoid importlib.reload().
    """

    conf_d = tmp_path / "conf.d"
    conf_disabled = tmp_path / "conf.disabled"
    conf_d.mkdir()
    conf_disabled.mkdir()

    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", conf_d)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", conf_d)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", conf_disabled)
    mocker.patch("vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False)
    
    mock_run = mocker.patch("vhost_helper.providers.nginx.run_elevated_command")
    
    provider = nginx.NginxProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")
    
    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "rhel_family")

    provider = nginx.NginxProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")

    provider.mock_run = mock_run
    provider.conf_d = conf_d
    provider.conf_disabled = conf_disabled
    return provider

def test_create_vhost_debian(patched_nginx_provider, vhost_config):
    """Test vhost creation on a Debian-like system."""
    patched_nginx_provider.create_vhost(vhost_config)
    calls = patched_nginx_provider.mock_run.call_args_list
    assert any("ln" in cmd.args[0] for cmd in calls)
    patched_nginx_provider.reload.assert_called_once()

def test_create_vhost_rhel(rhel_patched_nginx_provider, vhost_config):
    """Test vhost creation on a RHEL-like system (no symlinks)."""
    rhel_patched_nginx_provider.create_vhost(vhost_config)
    calls = rhel_patched_nginx_provider.mock_run.call_args_list
    assert not any("ln" in cmd.args[0] for cmd in calls)
    rhel_patched_nginx_provider.reload.assert_called_once()

def test_disable_vhost_debian(patched_nginx_provider, vhost_config):
    """Test vhost disabling on Debian (removes symlink)."""
    with patch("pathlib.Path.exists", return_value=True), patch("pathlib.Path.is_symlink", return_value=True):
        patched_nginx_provider.disable_vhost(vhost_config.domain)
    
    calls = patched_nginx_provider.mock_run.call_args_list
    assert any("rm" in cmd.args[0] for cmd in calls)
    patched_nginx_provider.reload.assert_called_once()

def test_disable_vhost_rhel(rhel_patched_nginx_provider, vhost_config):
    """Test vhost disabling on RHEL (moves file to conf.disabled)."""
    (rhel_patched_nginx_provider.conf_d / vhost_config.domain).touch()
    with patch("pathlib.Path.exists", return_value=True):
        rhel_patched_nginx_provider.disable_vhost(vhost_config.domain)
    
    calls = rhel_patched_nginx_provider.mock_run.call_args_list
    assert any("mv" in cmd.args[0] for cmd in calls)
    rhel_patched_nginx_provider.reload.assert_called_once()

def test_enable_vhost_debian(patched_nginx_provider, vhost_config):
    """Test vhost enabling on Debian (creates symlink)."""
    with patch("pathlib.Path.exists", side_effect=[True, False]): # config exists, link doesn't
        patched_nginx_provider.enable_vhost(vhost_config.domain)
    calls = patched_nginx_provider.mock_run.call_args_list
    assert any("ln" in cmd.args[0] for cmd in calls)
    patched_nginx_provider.reload.assert_called_once()

def test_enable_vhost_rhel(rhel_patched_nginx_provider, vhost_config):
    """Test vhost enabling on RHEL (moves file from conf.disabled)."""
    (rhel_patched_nginx_provider.conf_disabled / vhost_config.domain).touch()
    with patch("pathlib.Path.exists", side_effect=[True, False]): # exists in disabled, not in enabled
        rhel_patched_nginx_provider.enable_vhost(vhost_config.domain)

    calls = rhel_patched_nginx_provider.mock_run.call_args_list
    assert any("mv" in cmd.args[0] for cmd in calls)
    rhel_patched_nginx_provider.reload.assert_called_once()
