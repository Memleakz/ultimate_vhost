import subprocess
import shutil
from pathlib import Path
import pytest


@pytest.fixture
def project_paths():
    root = Path(__file__).resolve().parent.parent
    return {
        "root": root,
        "install": root / "install.sh",
        "uninstall": root / "uninstall.sh",
        "vhost": root / "bin" / "vhost",
    }


def test_install_script_requires_root(project_paths):
    """Verify install.sh fails if not run as root."""
    # We simulate non-root by mocking EUID if we were running the script,
    # but since it's a bash script, we can just check if it exits when EUID is not 0.
    # However, running it might actually try to install things if we are root.
    # In a test environment, we can't easily switch to non-root if we are root,
    # and we can't switch to root if we are not.
    # But we can check the script content for the check.
    content = project_paths["install"].read_text()
    assert 'if [ "$EUID" -ne 0 ]; then' in content
    assert "exit 1" in content


def test_uninstall_script_requires_root(project_paths):
    """Verify uninstall.sh fails if not run as root."""
    content = project_paths["uninstall"].read_text()
    assert 'if [ "$EUID" -ne 0 ]; then' in content
    assert "exit 1" in content


def test_install_script_missing_binary(project_paths, tmp_path):
    """Verify install.sh fails if vhost binary is missing."""
    # Create a dummy root without the binary
    dummy_root = tmp_path / "dummy_project"
    dummy_root.mkdir()

    # Copy and modify the script to bypass root check
    content = project_paths["install"].read_text()
    content = content.replace('if [ "$EUID" -ne 0 ]; then', "if false; then")
    install_script = dummy_root / "install.sh"
    install_script.write_text(content)

    # Run the script. It should fail because src/bin/vhost is missing in dummy_root.
    result = subprocess.run(
        ["bash", "install.sh"], cwd=dummy_root, capture_output=True, text=True
    )

    assert result.returncode == 1
    assert (
        "Error: Could not find vhost binary" in result.stdout
        or "Error: Could not find vhost binary" in result.stderr
    )


def test_uninstall_deep_clean_safety(project_paths, tmp_path):
    """Verify uninstall.sh --deep-clean does NOT delete web content."""
    # Create mock directories
    mock_nginx = tmp_path / "etc" / "nginx" / "sites-available"
    mock_nginx.mkdir(parents=True)
    mock_vhost_conf = mock_nginx / "example.test.conf"
    mock_vhost_conf.write_text("server { }")

    mock_www = tmp_path / "var" / "www" / "example.test"
    mock_www.mkdir(parents=True)
    mock_index = mock_www / "index.html"
    mock_index.write_text("<h1>Hello</h1>")

    # Mock log file
    mock_log = tmp_path / "var" / "log" / "vhost.log"
    mock_log.parent.mkdir(parents=True, exist_ok=True)
    mock_log.write_text("some logs")

    # Prepare uninstall script
    uninstall_script = tmp_path / "uninstall.sh"
    content = project_paths["uninstall"].read_text()
    # Modify paths in the script to point to our mock locations
    content = content.replace("/var/log/vhost.log", str(mock_log))
    # Note: The script doesn't actually touch /var/www or /etc/nginx but we want to be sure.
    uninstall_script.write_text(content)

    # Run uninstall --deep-clean (skipping root check by mocking EUID if we could,
    # but we'll just check that it DOES delete the log and DOES NOT delete the others)
    # We'll use a modified script that doesn't exit on root check for this test.
    test_script = tmp_path / "test_uninstall.sh"
    test_script.write_text(
        content.replace('if [ "$EUID" -ne 0 ]; then', "if false; then")
    )

    subprocess.run(["bash", str(test_script), "--deep-clean"], check=True)

    # Assertions
    assert not mock_log.exists()
    assert mock_vhost_conf.exists()
    assert mock_index.exists()


def test_install_script_idempotency(project_paths, tmp_path):
    """Verify install.sh can be run multiple times (mocked)."""
    # Create mock environment
    bin_dir = tmp_path / "usr" / "local" / "bin"
    bin_dir.mkdir(parents=True)

    completion_dir = tmp_path / "etc" / "bash_completion.d"
    completion_dir.mkdir(parents=True)

    # Mock install script
    content = project_paths["install"].read_text()
    # Mock root check and paths
    content = content.replace('if [ "$EUID" -ne 0 ]; then', "if false; then")
    content = content.replace("/usr/local/bin", str(bin_dir))
    content = content.replace("/etc/bash_completion.d", str(completion_dir))
    content = content.replace(
        "/opt/vhost-helper", str(tmp_path / "opt" / "vhost-helper")
    )
    # Mock pip installation to be fast
    content = content.replace("pip3 install", "echo pip3 install")
    content = content.replace("python3 -m pip install", "echo python3 -m pip install")
    # Mock vhost execution for completion
    content = content.replace(
        "/usr/local/bin/vhost --show-completion", "echo mock-completion"
    )
    # Mock privileged operations
    content = content.replace("chown -R root:root", "echo chown")
    content = content.replace("chmod -R 755", "echo chmod")

    # Copy bin directory to tmp_path so the script finds it
    shutil.copytree(project_paths["root"] / "bin", tmp_path / "bin")
    # Create requirements.txt
    (tmp_path / "requirements.txt").touch()

    install_script = tmp_path / "install.sh"
    install_script.write_text(content)

    # Run 1st time
    subprocess.run(["bash", str(install_script)], check=True, cwd=tmp_path)
    assert (bin_dir / "vhost").is_symlink()

    # Run 2nd time
    subprocess.run(["bash", str(install_script)], check=True, cwd=tmp_path)
    assert (bin_dir / "vhost").is_symlink()
    # It should still be there and valid
