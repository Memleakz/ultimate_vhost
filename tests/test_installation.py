import os
import subprocess
from pathlib import Path


def test_vhost_symlink_resolution():
    """
    Verify that the vhost binary can find its lib directory when called via a symlink.
    """
    # Find the deliverable root (where bin/ and lib/ live)
    current = Path(__file__).resolve().parent
    deliverable_root = None
    while current != current.root:
        if (current / "bin").exists() and (current / "lib").exists():
            deliverable_root = current
            break
        current = current.parent
    
    if not deliverable_root:
        # Fallback to parent of tests/ if not found
        deliverable_root = Path(__file__).resolve().parent.parent
        
    vhost_bin = deliverable_root / "bin" / "vhost"

    # Create a temporary directory for the symlink
    tmp_bin_dir = deliverable_root / "tests" / "tmp_bin"
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
    # Find the deliverable root
    current = Path(__file__).resolve().parent
    deliverable_root = None
    while current != current.root:
        if (current / "install.sh").exists():
            deliverable_root = current
            break
        current = current.parent
    
    if not deliverable_root:
        deliverable_root = Path(__file__).resolve().parent.parent

    assert (deliverable_root / "install.sh").exists()
    assert (deliverable_root / "uninstall.sh").exists()
    assert os.access(deliverable_root / "install.sh", os.X_OK)
    assert os.access(deliverable_root / "uninstall.sh", os.X_OK)
