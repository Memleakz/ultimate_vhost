import pytest
from unittest.mock import patch

from vhost_helper.models import VHostConfig, ServerType, RuntimeMode
from vhost_helper.providers import apache


@pytest.fixture
def vhost_config(tmp_path):
    """Provides a basic VHostConfig for tests."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    return VHostConfig(
        domain="test.local",
        document_root=str(doc_root),
        port=80,
        server_type=ServerType.APACHE,
        runtime=RuntimeMode.STATIC,
    )


@pytest.fixture
def patched_apache_provider(mocker, tmp_path):
    """
    Provides an ApacheProvider instance with patched dependencies for Debian.
    """
    sites_available = tmp_path / "sites-available"
    sites_enabled = tmp_path / "sites-enabled"
    sites_available.mkdir()
    sites_enabled.mkdir()

    mocker.patch(
        "vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", sites_available
    )
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "debian_family")
    mocker.patch(
        "vhost_helper.providers.apache.is_selinux_enforcing", return_value=False
    )

    mock_run = mocker.patch("vhost_helper.providers.apache.run_elevated_command")

    provider = apache.ApacheProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")

    provider.mock_run = mock_run
    provider.available = sites_available
    provider.enabled = sites_enabled
    return provider


@pytest.fixture
def rhel_patched_apache_provider(mocker, tmp_path):
    """
    Provides an ApacheProvider instance patched for a RHEL-like environment.
    """
    conf_d = tmp_path / "conf.d"
    conf_disabled = tmp_path / "conf.disabled"
    conf_d.mkdir()
    conf_disabled.mkdir()

    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", conf_d)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", conf_d)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", conf_disabled)
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "rhel_family")
    mocker.patch(
        "vhost_helper.providers.apache.is_selinux_enforcing", return_value=False
    )

    mock_run = mocker.patch("vhost_helper.providers.apache.run_elevated_command")

    provider = apache.ApacheProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")

    provider.mock_run = mock_run
    provider.conf_d = conf_d
    provider.conf_disabled = conf_disabled
    return provider


def test_create_vhost_debian(patched_apache_provider, vhost_config):
    """Test vhost creation on a Debian-like system."""
    patched_apache_provider.create_vhost(vhost_config)
    calls = patched_apache_provider.mock_run.call_args_list
    assert any("ln" in cmd.args[0] for cmd in calls)
    patched_apache_provider.reload.assert_called_once()


def test_create_vhost_rhel(rhel_patched_apache_provider, vhost_config):
    """Test vhost creation on a RHEL-like system (no symlinks)."""
    rhel_patched_apache_provider.create_vhost(vhost_config)
    calls = rhel_patched_apache_provider.mock_run.call_args_list
    assert not any("ln" in cmd.args[0] for cmd in calls)
    rhel_patched_apache_provider.reload.assert_called_once()


def test_remove_vhost(patched_apache_provider, vhost_config):
    """Test vhost removal."""
    (patched_apache_provider.available / (vhost_config.domain + ".conf")).touch()
    patched_apache_provider.remove_vhost(vhost_config.domain)
    calls = patched_apache_provider.mock_run.call_args_list
    assert any("rm" in cmd.args[0] for cmd in calls)
    patched_apache_provider.reload.assert_called_once()


def test_disable_vhost_debian(patched_apache_provider, vhost_config):
    """Test vhost disabling on Debian (removes symlink)."""
    (patched_apache_provider.enabled / (vhost_config.domain + ".conf")).touch()
    with patch("pathlib.Path.exists", return_value=True), patch(
        "pathlib.Path.is_symlink", return_value=True
    ):
        patched_apache_provider.disable_vhost(vhost_config.domain)

    calls = patched_apache_provider.mock_run.call_args_list
    assert any("rm" in cmd.args[0] for cmd in calls)
    patched_apache_provider.reload.assert_called_once()


def test_disable_vhost_rhel(rhel_patched_apache_provider, vhost_config):
    """Test vhost disabling on RHEL (moves file to conf.disabled)."""
    (rhel_patched_apache_provider.conf_d / (vhost_config.domain + ".conf")).touch()
    with patch("pathlib.Path.exists", return_value=True):
        rhel_patched_apache_provider.disable_vhost(vhost_config.domain)

    calls = rhel_patched_apache_provider.mock_run.call_args_list
    assert any("mv" in cmd.args[0] for cmd in calls)
    rhel_patched_apache_provider.reload.assert_called_once()


def test_enable_vhost_debian(patched_apache_provider, vhost_config):
    """Test vhost enabling on Debian (creates symlink)."""
    with patch(
        "pathlib.Path.exists", side_effect=[True, False]
    ):  # config exists, link doesn't
        patched_apache_provider.enable_vhost(vhost_config.domain)
    calls = patched_apache_provider.mock_run.call_args_list
    assert any("ln" in cmd.args[0] for cmd in calls)
    patched_apache_provider.reload.assert_called_once()


def test_enable_vhost_rhel(rhel_patched_apache_provider, vhost_config):
    """Test vhost enabling on RHEL (moves file from conf.disabled)."""
    (
        rhel_patched_apache_provider.conf_disabled / (vhost_config.domain + ".conf")
    ).touch()
    with patch(
        "pathlib.Path.exists", side_effect=[True, False]
    ):  # exists in disabled, not in enabled
        rhel_patched_apache_provider.enable_vhost(vhost_config.domain)

    calls = rhel_patched_apache_provider.mock_run.call_args_list
    assert any("mv" in cmd.args[0] for cmd in calls)
    rhel_patched_apache_provider.reload.assert_called_once()


def test_validate_config_debian(patched_apache_provider):
    """Test config validation command on Debian."""
    patched_apache_provider.os_family = "debian_family"
    # The fixture already patches it, but we need to call the actual method
    # and check if it called run_elevated_command.
    # We un-mock the method but keep the utility mocked.
    with patch.object(
        apache.ApacheProvider, "validate_config", autospec=True
    ) as mock_validate:
        mock_validate.side_effect = apache.ApacheProvider.validate_config
        # This is getting complicated. Let's just mock run_elevated_command in the test.
        pass

    # Simple approach:
    provider = apache.ApacheProvider()
    provider.os_family = "debian_family"
    with patch("vhost_helper.providers.apache.run_elevated_command") as mock_run:
        provider.validate_config()
        mock_run.assert_called_once()
        assert "apache2ctl" in mock_run.call_args[0][0]


def test_validate_config_rhel(rhel_patched_apache_provider):
    """Test config validation command on RHEL."""
    provider = apache.ApacheProvider()
    provider.os_family = "rhel_family"
    with patch("vhost_helper.providers.apache.run_elevated_command") as mock_run:
        provider.validate_config()
        mock_run.assert_called_once()
        assert "httpd" in mock_run.call_args[0][0]
        assert "-t" in mock_run.call_args[0][0]
