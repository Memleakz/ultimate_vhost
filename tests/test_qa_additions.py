import pytest
import os
import tempfile
import subprocess
from pathlib import Path
from vhost_helper.hostfile import add_entry, remove_entry
from vhost_helper.providers.nginx import NginxProvider
from vhost_helper.os_detector import get_os_info
from vhost_helper.models import VHostConfig, ServerType, OSInfo
from typer.testing import CliRunner
from vhost_helper.main import app

runner = CliRunner()

# Absolute path to detect_os.sh, resolved relative to this test file so the
# tests work regardless of the working directory pytest is invoked from.
_DETECT_OS_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "detect_os.sh"


@pytest.fixture
def temp_hosts_file():
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write("127.0.0.1\tlocalhost\n")
        tmp_path = tmp.name

    import vhost_helper.hostfile

    old_hosts = vhost_helper.hostfile.HOSTS_FILE
    vhost_helper.hostfile.HOSTS_FILE = tmp_path
    yield tmp_path
    vhost_helper.hostfile.HOSTS_FILE = old_hosts
    if os.path.exists(tmp_path):
        os.remove(tmp_path)


def test_hostfile_add_duplicate(temp_hosts_file, mocker):
    # Mock Popen
    mock_popen = mocker.MagicMock()
    mock_popen.communicate.returncode = 0
    mocker.patch("subprocess.Popen", return_value=mock_popen)

    # Add entry that already exists (with same IP)
    add_entry("127.0.0.1", "localhost")

    # Check that Popen was NOT called (because it already exists)
    assert not subprocess.Popen.called


def test_hostfile_add_different_ip(temp_hosts_file, mocker):
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")

    # Add localhost but with different IP — triggers remove then add
    add_entry("127.0.0.2", "localhost")

    # subprocess.run called at least once (remove + add via tee)
    assert mock_run.called


def test_nginx_rollback_on_failure(mocker):
    # Mock NGINX directories
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        sites_available = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_available.mkdir()
        sites_enabled.mkdir()

        mocker.patch(
            "vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_available
        )
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled)

        # Mock subprocess.run to succeed for mv and ln but fail for reload or we mock validate_config
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        )

        # Mock validate_config to fail
        mocker.patch.object(NginxProvider, "validate_config", return_value=False)
        # Mock remove_vhost to see if it's called
        mocker.patch.object(NginxProvider, "remove_vhost")

        provider = NginxProvider()
        config = VHostConfig(
            domain="fail.test",
            document_root=Path("/tmp"),
            port=80,
            server_type=ServerType.NGINX,
        )

        with pytest.raises(RuntimeError, match="Nginx configuration validation failed"):
            provider.create_vhost(config)

        provider.remove_vhost.assert_called_with("fail.test")


def test_os_detector_failure(mocker):
    # Mock subprocess.run to fail
    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.CalledProcessError(
            1, "detect_os.sh", stderr="OS not supported"
        ),
    )

    with pytest.raises(RuntimeError, match="OS detection failed"):
        get_os_info()


def test_cli_create_nonexistent_dir():
    # Attempt to create a vhost with a non-existent document root
    result = runner.invoke(app, ["create", "test.test", "/nonexistent/path"])
    assert result.exit_code == 1
    assert "Document root" in result.stdout
    assert "does not exist" in result.stdout


def test_cli_info_system(mocker):
    # Mock get_os_info
    mocker.patch(
        "vhost_helper.main.get_os_info",
        return_value=OSInfo(id="ubuntu", version="22.04", family="debian"),
    )

    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0
    assert "ubuntu" in result.stdout
    assert "22.04" in result.stdout


def test_cli_list_no_sites(mocker):
    # Mock NGINX_SITES_ENABLED to point to an empty temp dir
    with tempfile.TemporaryDirectory() as tmp_dir:
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", Path(tmp_dir))
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "Managed Virtual Hosts" in result.stdout


def test_domain_validation_invalid():
    # Attempt to create a vhost with an invalid domain
    result = runner.invoke(app, ["create", "evil;rm", "/tmp"])
    assert result.exit_code == 1
    # Check for the custom validation error message
    assert "Invalid domain format" in result.stdout


def test_hostfile_shell_injection_prevention(temp_hosts_file, mocker):
    # Check that the tee command is passed as a list (not a shell string) to subprocess.run
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")

    add_entry("127.0.0.1", "pwned.test")

    import vhost_helper.hostfile

    # Find the tee -a call among all subprocess.run calls
    tee_call = next(c for c in mock_run.call_args_list if "tee" in c[0][0])
    args = tee_call[0][0]
    assert "tee" in args
    assert "-a" in args
    assert str(vhost_helper.hostfile.HOSTS_FILE) in args


def test_vhost_config_validation():
    # Invalid port
    with pytest.raises(ValueError):
        VHostConfig(domain="test.test", document_root=Path("/tmp"), port=0)
    with pytest.raises(ValueError):
        VHostConfig(domain="test.test", document_root=Path("/tmp"), port=65536)

    # Document root is a file, not a directory
    with tempfile.NamedTemporaryFile() as tmp:
        with pytest.raises(ValueError, match="must be a directory"):
            VHostConfig(domain="test.test", document_root=Path(tmp.name))


def test_cli_remove_vhost_success(mocker, tmp_path):
    # Mock hostfile.remove_entry and NginxProvider.remove_vhost
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch.object(NginxProvider, "remove_vhost")

    available_dir = tmp_path / "nginx-available"
    available_dir.mkdir()
    enabled_dir = tmp_path / "nginx-enabled"
    enabled_dir.mkdir()
    (available_dir / "test.test.conf").touch()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available_dir)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled_dir)
    mocker.patch(
        "vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-available"
    )
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled")
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

    result = runner.invoke(app, ["remove", "test.test", "--force"])
    assert result.exit_code == 0
    assert "Success!" in result.stdout
    assert "test.test" in result.stdout


def test_cli_remove_vhost_aborted(mocker):
    result = runner.invoke(app, ["remove", "test.test"], input="n\n")
    assert result.exit_code == 1  # Typer.Abort exits with 1
    assert "Aborted" in result.stdout


def test_cli_list_vhosts(mocker):
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        available = tmp_path / "sites-available"
        enabled = tmp_path / "sites-enabled"
        available.mkdir()
        enabled.mkdir()

        (available / "site1.test.conf").touch()
        (available / "site2.test.conf").touch()
        # Enable site1
        (enabled / "site1.test.conf").touch()

        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
        mocker.patch(
            "vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-available"
        )
        mocker.patch(
            "vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled"
        )
        mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "site1.test" in result.stdout
        assert "site2.test" in result.stdout
        assert "Enabled" in result.stdout
        assert "Disabled" in result.stdout


def test_cli_info_domain_not_found(mocker, tmp_path):
    # Mock NGINX_SITES_AVAILABLE and APACHE_SITES_AVAILABLE to empty temp dirs
    nginx_avail = tmp_path / "nginx-available"
    nginx_avail.mkdir()
    apache_avail = tmp_path / "apache-available"
    apache_avail.mkdir()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", nginx_avail)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nginx-enabled")
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", apache_avail)
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled")
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

    result = runner.invoke(app, ["info", "nonexistent.test"])
    assert result.exit_code == 1
    assert "No configuration found" in result.stdout


def test_nginx_rollback_on_reload_failure(mocker):
    # Mock NGINX directories
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        sites_available = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_available.mkdir()
        sites_enabled.mkdir()

        mocker.patch(
            "vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_available
        )
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled)

        # Mock subprocess.run
        mocker.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        )

        # Mock validate_config to succeed but reload to fail
        mocker.patch.object(NginxProvider, "validate_config", return_value=True)
        mocker.patch.object(
            NginxProvider, "reload", side_effect=RuntimeError("Reload failed")
        )
        # Mock remove_vhost to see if it's called
        mocker.patch.object(NginxProvider, "remove_vhost")

        provider = NginxProvider()
        config = VHostConfig(
            domain="reload-fail.test",
            document_root=Path("/tmp"),
            port=80,
            server_type=ServerType.NGINX,
        )

        with pytest.raises(
            RuntimeError, match="Nginx reload failed, rollback performed"
        ):
            provider.create_vhost(config)

        # Verify rollback was called
        provider.remove_vhost.assert_called_with("reload-fail.test")


def test_detect_os_script_ubuntu(mocker):
    # Mock /etc/os-release
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write('ID=ubuntu\nVERSION_ID="22.04"\n')
        tmp_path = tmp.name

    try:
        script_content = _DETECT_OS_SCRIPT.read_text()
        modified_script = script_content.replace("/etc/os-release", tmp_path)

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".sh"
        ) as tmp_script:
            tmp_script.write(modified_script)
            tmp_script_path = tmp_script.name

        os.chmod(tmp_script_path, 0o755)

        result = subprocess.run([tmp_script_path], capture_output=True, text=True)
        assert result.returncode == 0
        assert "ID=ubuntu" in result.stdout
        assert "VERSION=22.04" in result.stdout

        os.remove(tmp_script_path)
    finally:
        os.remove(tmp_path)


def test_detect_os_script_unsupported(mocker):
    # Mock /etc/os-release with an unsupported OS
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write('ID=arch\nVERSION_ID="rolling"\n')
        tmp_path = tmp.name

    try:
        script_content = _DETECT_OS_SCRIPT.read_text()
        modified_script = script_content.replace("/etc/os-release", tmp_path)

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".sh"
        ) as tmp_script:
            tmp_script.write(modified_script)
            tmp_script_path = tmp_script.name

        os.chmod(tmp_script_path, 0o755)

        result = subprocess.run([tmp_script_path], capture_output=True, text=True)
        assert result.returncode != 0
        assert "Error" in result.stderr

        os.remove(tmp_script_path)
    finally:
        os.remove(tmp_path)


def test_hostfile_add_entry_sudo_fail(temp_hosts_file, mocker):
    mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=1),
    )
    mocker.patch("vhost_helper.utils._console")

    with pytest.raises(RuntimeError, match="Failed to add hostfile entry"):
        add_entry("127.0.0.1", "fail.test")


def test_hostfile_remove_entry_sudo_fail(temp_hosts_file, mocker):
    mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=1),
    )
    mocker.patch("vhost_helper.utils._console")
    # Mock sudo prefix to trigger tee path
    mocker.patch("vhost_helper.hostfile.get_sudo_prefix", return_value=["sudo"])

    # Ensure the entry exists in the temp file
    with open(temp_hosts_file, "a") as f:
        f.write("127.0.0.1\tfail.test\n")

    with pytest.raises(RuntimeError, match="Failed to remove hostfile entry"):
        remove_entry("fail.test")


def test_hostfile_very_long_domain(temp_hosts_file, mocker):
    # Max domain length is 253 characters
    long_domain = "a" * 250 + ".test"

    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")

    # This should pass validation (it's < 253)
    add_entry("127.0.0.1", long_domain)
    assert mock_run.called


def test_detect_os_script_debian_legacy(mocker):
    # Mock /etc/os-release NOT existing
    # Mock /etc/redhat-release NOT existing
    # Mock /etc/debian_version existing
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp_deb:
        tmp_deb.write("11.5\n")
        tmp_deb_path = tmp_deb.name

    try:
        script_content = _DETECT_OS_SCRIPT.read_text()
        # Replace all with non-existent or temp paths
        modified_script = script_content.replace(
            "/etc/os-release", "/tmp/nonexistent-os-release"
        )
        modified_script = modified_script.replace(
            "/etc/redhat-release", "/tmp/nonexistent-redhat-release"
        )
        modified_script = modified_script.replace("/etc/debian_version", tmp_deb_path)

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".sh"
        ) as tmp_script:
            tmp_script.write(modified_script)
            tmp_script_path = tmp_script.name

        os.chmod(tmp_script_path, 0o755)

        result = subprocess.run([tmp_script_path], capture_output=True, text=True)
        assert result.returncode == 0
        assert "ID=debian" in result.stdout
        assert "VERSION=11.5" in result.stdout

        os.remove(tmp_script_path)
    finally:
        os.remove(tmp_deb_path)


def test_detect_os_script_rhel_legacy(mocker):
    # Mock /etc/os-release NOT existing
    # Mock /etc/redhat-release existing
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp_rhel:
        tmp_rhel.write("Red Hat Enterprise Linux release 8.6 (Ootpa)\n")
        tmp_rhel_path = tmp_rhel.name

    try:
        script_content = _DETECT_OS_SCRIPT.read_text()
        modified_script = script_content.replace(
            "/etc/os-release", "/tmp/nonexistent-os-release"
        )
        modified_script = modified_script.replace("/etc/redhat-release", tmp_rhel_path)

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".sh"
        ) as tmp_script:
            tmp_script.write(modified_script)
            tmp_script_path = tmp_script.name

        os.chmod(tmp_script_path, 0o755)

        result = subprocess.run([tmp_script_path], capture_output=True, text=True)
        assert result.returncode == 0
        assert "ID=rhel" in result.stdout
        assert "VERSION=8.6" in result.stdout

        os.remove(tmp_script_path)
    finally:
        os.remove(tmp_rhel_path)


def test_detect_os_script_centos(mocker):
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write('ID=centos\nVERSION_ID="7"\n')
        tmp_path = tmp.name

    try:
        script_content = _DETECT_OS_SCRIPT.read_text()
        modified_script = script_content.replace("/etc/os-release", tmp_path)

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".sh"
        ) as tmp_script:
            tmp_script.write(modified_script)
            tmp_script_path = tmp_script.name

        os.chmod(tmp_script_path, 0o755)

        result = subprocess.run([tmp_script_path], capture_output=True, text=True)
        assert result.returncode == 0
        assert "ID=centos" in result.stdout
        assert "VERSION=7" in result.stdout

        os.remove(tmp_script_path)
    finally:
        os.remove(tmp_path)


def test_detect_os_script_fedora(mocker):
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write('ID=fedora\nVERSION_ID="36"\n')
        tmp_path = tmp.name

    try:
        script_content = _DETECT_OS_SCRIPT.read_text()
        modified_script = script_content.replace("/etc/os-release", tmp_path)

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".sh"
        ) as tmp_script:
            tmp_script.write(modified_script)
            tmp_script_path = tmp_script.name

        os.chmod(tmp_script_path, 0o755)

        result = subprocess.run([tmp_script_path], capture_output=True, text=True)
        assert result.returncode == 0
        assert "ID=fedora" in result.stdout
        assert "VERSION=36" in result.stdout

        os.remove(tmp_script_path)
    finally:
        os.remove(tmp_path)


def test_detect_os_script_no_files_fail(mocker):
    try:
        script_content = _DETECT_OS_SCRIPT.read_text()
        # Mock all possible files as missing
        modified_script = script_content.replace(
            "/etc/os-release", "/tmp/nonexistent-os-release"
        )
        modified_script = modified_script.replace(
            "/etc/redhat-release", "/tmp/nonexistent-redhat-release"
        )
        modified_script = modified_script.replace(
            "/etc/debian_version", "/tmp/nonexistent-debian-version"
        )

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".sh"
        ) as tmp_script:
            tmp_script.write(modified_script)
            tmp_script_path = tmp_script.name

        os.chmod(tmp_script_path, 0o755)

        result = subprocess.run([tmp_script_path], capture_output=True, text=True)
        assert result.returncode != 0

        os.remove(tmp_script_path)
    finally:
        pass


def test_project_structure_exists():
    root = Path(__file__).resolve().parent.parent
    assert (root / "bin").is_dir()
    assert (root / "lib").is_dir()
    assert (root / "templates").is_dir()
    assert (root / "bin" / "detect_os.sh").exists()
    assert os.access(root / "bin" / "detect_os.sh", os.X_OK)


def test_detect_os_script_fallback_version(mocker):
    # Mock /etc/os-release with ID but no VERSION_ID, using VERSION instead
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write('ID=ubuntu\nVERSION="20.04.4 LTS (Focal Fossa)"\n')
        tmp_path = tmp.name

    try:
        script_content = _DETECT_OS_SCRIPT.read_text()
        modified_script = script_content.replace("/etc/os-release", tmp_path)

        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix=".sh"
        ) as tmp_script:
            tmp_script.write(modified_script)
            tmp_script_path = tmp_script.name

        os.chmod(tmp_script_path, 0o755)

        result = subprocess.run([tmp_script_path], capture_output=True, text=True)
        assert result.returncode == 0
        assert "ID=ubuntu" in result.stdout
        assert "VERSION=20.04.4 LTS (Focal Fossa)" in result.stdout

        os.remove(tmp_script_path)
    finally:
        os.remove(tmp_path)


def test_nginx_provider_invalid_template_dir(mocker):
    """
    Tests that the provider raises an error if it cannot find a template
    because the template directories do not exist.
    """
    from vhost_helper.providers.nginx import NginxProvider

    # Patch the config directories to point to paths that won't exist
    mocker.patch(
        "vhost_helper.providers.nginx.APP_TEMPLATES_DIR",
        Path("/nonexistent/app/templates"),
    )
    mocker.patch(
        "vhost_helper.providers.nginx.USER_TEMPLATES_DIR",
        Path("/nonexistent/user/templates"),
    )

    provider = NginxProvider()
    # The provider should only fail when a template is requested
    with pytest.raises(FileNotFoundError, match="Template 'default' not found"):
        provider._get_template("default")


def test_cli_info_no_information_disclosure(mocker):
    # Mock NGINX_SITES_AVAILABLE
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        available = tmp_path / "sites-available"
        enabled = tmp_path / "sites-enabled"
        available.mkdir()
        enabled.mkdir()

        # Create a vhost config with a sensitive comment
        config_content = """
server {
    listen 80;
    server_name "secret.test";
    root "/var/www/secret";
    # SECRET_KEY=dont_show_this
}
"""
        config_file = available / "secret.test.conf"
        config_file.write_text(config_content)

        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)

        result = runner.invoke(app, ["info", "secret.test"])
        assert result.exit_code == 0
        assert "secret.test" in result.stdout
        assert "/var/www/secret" in result.stdout
        # The secret comment should NOT be in the output
        assert "SECRET_KEY" not in result.stdout
        assert "dont_show_this" not in result.stdout


def test_hostfile_remove_entry_boundary(mocker):
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")

    # We need a real file to read from
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write("127.0.0.1\tsite.test\n")
        tmp_path = tmp.name

    try:
        mocker.patch("vhost_helper.hostfile.HOSTS_FILE", tmp_path)
        mocker.patch("vhost_helper.hostfile.get_sudo_prefix", return_value=["sudo"])

        remove_entry("site.test")

        # Check if tee was used
        args = mock_run.call_args[0][0]
        assert "tee" in args
        assert "sed" not in args
    finally:
        os.remove(tmp_path)
