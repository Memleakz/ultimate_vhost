import os
import subprocess
from pathlib import Path


def test_vhost_symlink_resolution():
    """
    Verify that the vhost binary can find its lib directory when called via a symlink.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    vhost_bin = project_root / "src" / "bin" / "vhost"

    # Create a temporary directory for the symlink
    tmp_bin_dir = project_root / "src" / "tests" / "tmp_bin"
    tmp_bin_dir.mkdir(exist_ok=True)
    symlink_path = tmp_bin_dir / "vhost_symlink"

    if symlink_path.exists():
        symlink_path.unlink()

    try:
        # Create symlink
        os.symlink(vhost_bin, symlink_path)

        # Try to run the symlink with --help
        # We need to make sure the environment has the necessary dependencies
        # or at least that it doesn't fail on import.
        # We'll use the current python interpreter.
        result = subprocess.run(
            [str(symlink_path), "--help"],
            capture_output=True,
            text=True,
            env={**os.environ, "VHOST_TEST_MODE": "1"},
        )

        assert result.returncode == 0
        assert "vhost" in result.stdout.lower()

    finally:
        # Cleanup
        if symlink_path.exists():
            symlink_path.unlink()
        if tmp_bin_dir.exists():
            tmp_bin_dir.rmdir()


def test_installer_scripts_existence():
    """
    Verify that install.sh and uninstall.sh exist in the root.
    """
    project_root = Path(__file__).resolve().parent.parent
    assert (project_root / "install.sh").exists()
    assert (project_root / "uninstall.sh").exists()
    assert os.access(project_root / "install.sh", os.X_OK)
    assert os.access(project_root / "uninstall.sh", os.X_OK)
