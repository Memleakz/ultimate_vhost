import pytest
import os
import tempfile
from pathlib import Path
from typer.testing import CliRunner
from vhost_helper.main import app
import vhost_helper.main
import vhost_helper.providers.nginx
import vhost_helper.hostfile

runner = CliRunner()


@pytest.fixture
def mock_nginx_setup(mocker):
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        available = tmp_path / "sites-available"
        enabled = tmp_path / "sites-enabled"
        available.mkdir()
        enabled.mkdir()

        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)

        yield available, enabled


def test_info_command_with_existing_domain(mock_nginx_setup, mocker):
    available, enabled = mock_nginx_setup
    domain = "testsite.test"
    config_file = available / (domain + ".conf")
    config_file.write_text("""
server {
    listen 80;
    server_name testsite.test;
    root /var/www/testsite;
}
""")

    # This should NOT crash now that 're' is imported
    result = runner.invoke(app, ["info", domain])
    assert result.exit_code == 0
    assert "testsite.test" in result.stdout
    assert "/var/www/testsite" in result.stdout
    assert "80" in result.stdout


def test_domain_validation_injection_prevention():
    # Attempt injection in create
    result = runner.invoke(app, ["create", "evil.test; rm -rf /", "/tmp"])
    assert result.exit_code == 1
    assert "Invalid domain format" in result.stdout

    # Attempt injection in remove
    result = runner.invoke(app, ["remove", "evil.test; rm -rf /", "--force"])
    assert result.exit_code == 1
    assert "Invalid domain format" in result.stdout

    # Attempt injection in info
    result = runner.invoke(app, ["info", "evil.test; rm -rf /"])
    assert result.exit_code == 1
    assert "Invalid domain format" in result.stdout


def test_list_command_filters_invalid_files(mock_nginx_setup, mocker):
    available, enabled = mock_nginx_setup
    # Create valid configuration files in sites-available (with .conf extension)
    (available / "valid.test.conf").touch()
    (available / "invalid_domain").touch()
    (available / "evil;rm").touch()

    # Enable the valid one
    (enabled / "valid.test.conf").touch()

    result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    assert "valid.test" in result.stdout
    assert "invalid_domain" not in result.stdout
    assert "evil;rm" not in result.stdout
    assert "Enabled" in result.stdout


def test_hostfile_remove_entry_escaping(mocker):
    import subprocess

    # Mock subprocess.run
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")

    # We need a real file to read from
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write("127.0.0.1\ttest.test\n")
        tmp_path = tmp.name

    try:
        mocker.patch("vhost_helper.hostfile.HOSTS_FILE", tmp_path)
        # Mock get_sudo_prefix to return something non-empty so tee is used
        mocker.patch("vhost_helper.hostfile.get_sudo_prefix", return_value=["sudo"])

        vhost_helper.hostfile.remove_entry("test.test")

        # Check if tee was used
        args = mock_run.call_args[0][0]
        assert "tee" in args
        assert "sed" not in args
        assert "-i" not in args
    finally:
        os.remove(tmp_path)
