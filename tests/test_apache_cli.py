import pytest
from typer.testing import CliRunner
from vhost_helper.main import app
import vhost_helper.main
import vhost_helper.providers.apache

runner = CliRunner()


@pytest.fixture
def mock_apache_setup(mocker, tmp_path):
    available = tmp_path / "apache-available"
    enabled = tmp_path / "apache-enabled"
    available.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", enabled)

    mocker.patch("vhost_helper.main.is_apache_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=False)
    mocker.patch("vhost_helper.main.is_apache_running", return_value=True)

    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch("vhost_helper.main.preflight_sudo_check")

    # Mock Provider methods
    mocker.patch("vhost_helper.providers.apache.ApacheProvider.create_vhost")
    mocker.patch("vhost_helper.providers.apache.ApacheProvider.remove_vhost")
    mocker.patch("vhost_helper.providers.apache.ApacheProvider.enable_vhost")
    mocker.patch("vhost_helper.providers.apache.ApacheProvider.disable_vhost")

    return available, enabled


def test_create_apache_vhost(mock_apache_setup, tmp_path):
    available, enabled = mock_apache_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(
        app, ["create", "apache.test", str(doc_root), "--provider", "apache"]
    )
    assert result.exit_code == 0
    assert "Provisioning 'apache.test'..." in result.stdout
    assert "Apache configuration generated" in result.stdout
    vhost_helper.providers.apache.ApacheProvider.create_vhost.assert_called_once()


def test_remove_apache_vhost(mock_apache_setup):
    available, enabled = mock_apache_setup
    domain = "apache.test"
    (available / (domain + ".conf")).touch()

    result = runner.invoke(app, ["remove", domain, "--force"])
    assert result.exit_code == 0
    assert f"Removing '{domain}'..." in result.stdout
    vhost_helper.providers.apache.ApacheProvider.remove_vhost.assert_called_once()


def test_list_apache_vhosts(mock_apache_setup):
    available, enabled = mock_apache_setup
    (available / "site1.test.conf").write_text("DocumentRoot /var/www/site1")
    (enabled / "site1.test.conf").touch()
    (available / "site2.test.conf").write_text("DocumentRoot /var/www/site2")

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "site1.test" in result.stdout
    assert "/var/www/site1" in result.stdout
    assert "Apache" in result.stdout
    assert "Enabled" in result.stdout
    assert "site2.test" in result.stdout
    assert "Disabled" in result.stdout


def test_info_apache_vhost(mock_apache_setup):
    available, enabled = mock_apache_setup
    domain = "apache.test"
    (available / (domain + ".conf")).write_text(f"""
<VirtualHost *:80>
    ServerName {domain}
    DocumentRoot /var/www/apache
</VirtualHost>
""")

    result = runner.invoke(app, ["info", domain])
    assert result.exit_code == 0
    assert domain in result.stdout
    assert "/var/www/apache" in result.stdout
    assert "Apache" in result.stdout
