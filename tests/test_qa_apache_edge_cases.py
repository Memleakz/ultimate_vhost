import pytest
from unittest.mock import patch, MagicMock
from vhost_helper.providers.apache import ApacheProvider
from vhost_helper.models import VHostConfig, RuntimeMode, ServerType


@pytest.fixture
def mock_apache_dirs(tmp_path):
    available = tmp_path / "apache2" / "sites-available"
    enabled = tmp_path / "apache2" / "sites-enabled"
    available.mkdir(parents=True)
    enabled.mkdir(parents=True)

    with patch(
        "vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", available
    ), patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", enabled), patch(
        "vhost_helper.providers.apache.detected_os_family", "debian_family"
    ):
        yield available, enabled


@pytest.fixture
def mock_apache_dirs_rhel(tmp_path):
    conf_d = tmp_path / "httpd" / "conf.d"
    disabled = tmp_path / "httpd" / "conf.disabled"
    conf_d.mkdir(parents=True)
    disabled.mkdir(parents=True)

    with patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", conf_d), patch(
        "vhost_helper.providers.apache.APACHE_SITES_ENABLED", conf_d
    ), patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", disabled), patch(
        "vhost_helper.providers.apache.detected_os_family", "rhel_family"
    ):
        yield conf_d, disabled


def test_apache_template_log_dir_rhel(mock_apache_dirs_rhel, tmp_path):
    """
    Test that the Apache template uses absolute paths or safe defaults for logs on RHEL,
    instead of relying on Debian-specific ${APACHE_LOG_DIR}.
    """
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    config = VHostConfig(
        domain="test.site",
        document_root=doc_root,
        server_type=ServerType.APACHE,
        runtime=RuntimeMode.STATIC,
    )

    provider = ApacheProvider()

    with patch("vhost_helper.providers.apache.run_elevated_command") as mock_run, patch(
        "vhost_helper.providers.apache.get_sudo_prefix", return_value=[]
    ), patch("vhost_helper.providers.apache.is_selinux_enforcing", return_value=False):

        # Mock run_elevated_command to actually move the file
        def side_effect(cmd, **kwargs):
            if cmd[0] == "mv":
                import shutil

                shutil.move(cmd[1], cmd[2])
            return MagicMock()

        mock_run.side_effect = side_effect

        provider.create_vhost(config, service_running=False)

    config_file = mock_apache_dirs_rhel[0] / "test.site.conf"
    assert config_file.exists()
    content = config_file.read_text()

    # If it contains ${APACHE_LOG_DIR}, it might fail on RHEL unless explicitly defined
    # Actually, RHEL's httpd often doesn't define this.
    # Standard httpd on RHEL uses /var/log/httpd/
    if "${APACHE_LOG_DIR}" in content:
        pytest.fail(
            "Template uses ${APACHE_LOG_DIR} which is Debian-specific and likely fails on RHEL/Fedora."
        )


def test_apache_php_socket_format(mock_apache_dirs, tmp_path):
    """
    Test that the Apache template correctly formats the PHP socket for SetHandler.
    It should probably be proxy:unix:/path/to/socket|fcgi://localhost
    """
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    socket_path = "/run/php/php-fpm.sock"
    config = VHostConfig(
        domain="php.site",
        document_root=doc_root,
        server_type=ServerType.APACHE,
        runtime=RuntimeMode.PHP,
        php_socket=socket_path,
    )

    provider = ApacheProvider()

    with patch("vhost_helper.providers.apache.run_elevated_command") as mock_run, patch(
        "vhost_helper.providers.apache.get_sudo_prefix", return_value=[]
    ), patch("vhost_helper.providers.apache.is_selinux_enforcing", return_value=False):

        # Mock run_elevated_command to actually move the file
        def side_effect(cmd, **kwargs):
            if cmd[0] == "mv":
                import shutil

                shutil.move(cmd[1], cmd[2])
            return MagicMock()

        mock_run.side_effect = side_effect

        provider.create_vhost(config, service_running=False)

    config_file = mock_apache_dirs[0] / "php.site.conf"
    assert config_file.exists()
    content = config_file.read_text()

    # Check for correct Apache PHP-FPM syntax
    assert f"proxy:unix:{socket_path}|fcgi://localhost" in content


def test_apache_python_runtime(mock_apache_dirs, tmp_path):
    """Test that the Apache template correctly configures ProxyPass for Python."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    config = VHostConfig(
        domain="python.site",
        document_root=doc_root,
        server_type=ServerType.APACHE,
        runtime=RuntimeMode.PYTHON,
        python_port=9000,
    )

    provider = ApacheProvider()

    with patch("vhost_helper.providers.apache.run_elevated_command") as mock_run, patch(
        "vhost_helper.providers.apache.get_sudo_prefix", return_value=[]
    ), patch("vhost_helper.providers.apache.is_selinux_enforcing", return_value=False):

        def side_effect(cmd, **kwargs):
            if cmd[0] == "mv":
                import shutil

                shutil.move(cmd[1], cmd[2])
            return MagicMock()

        mock_run.side_effect = side_effect

        provider.create_vhost(config, service_running=False)

    config_file = mock_apache_dirs[0] / "python.site.conf"
    content = config_file.read_text()

    assert "ProxyPass / http://127.0.0.1:9000/" in content
    assert "ProxyPassReverse / http://127.0.0.1:9000/" in content


def test_apache_canonical_redirect(mock_apache_dirs, tmp_path):
    """Test that the Apache template generates a redirect block for www/non-www."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    config = VHostConfig(
        domain="mysite.test",
        document_root=doc_root,
        server_type=ServerType.APACHE,
        runtime=RuntimeMode.STATIC,
    )

    provider = ApacheProvider()

    with patch("vhost_helper.providers.apache.run_elevated_command") as mock_run, patch(
        "vhost_helper.providers.apache.get_sudo_prefix", return_value=[]
    ), patch("vhost_helper.providers.apache.is_selinux_enforcing", return_value=False):

        def side_effect(cmd, **kwargs):
            if cmd[0] == "mv":
                import shutil

                shutil.move(cmd[1], cmd[2])
            return MagicMock()

        mock_run.side_effect = side_effect

        provider.create_vhost(config, service_running=False)

    config_file = mock_apache_dirs[0] / "mysite.test.conf"
    content = config_file.read_text()

    assert "ServerName www.mysite.test" in content
    assert "Redirect permanent / http://mysite.test/" in content
