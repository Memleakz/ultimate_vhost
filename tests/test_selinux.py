import subprocess
from unittest.mock import patch, MagicMock

from vhost_helper import os_detector


def test_is_selinux_enforcing_when_command_does_not_exist():
    """
    Verify is_selinux_enforcing() returns False if `getenforce` is not in PATH.
    """
    with patch("shutil.which", return_value=None) as mock_which:
        assert not os_detector.is_selinux_enforcing()
        mock_which.assert_called_once_with("getenforce")


@patch("subprocess.run")
@patch("shutil.which", return_value="/usr/sbin/getenforce")
def test_is_selinux_enforcing_when_enforcing(mock_which, mock_run):
    """
    Verify is_selinux_enforcing() returns True when `getenforce` returns 'Enforcing'.
    """
    mock_run.return_value = MagicMock(stdout="Enforcing", returncode=0)
    assert os_detector.is_selinux_enforcing()
    mock_which.assert_called_once_with("getenforce")
    mock_run.assert_called_once_with(["getenforce"], capture_output=True, text=True)


@patch("subprocess.run")
@patch("shutil.which", return_value="/usr/sbin/getenforce")
def test_is_selinux_enforcing_when_permissive(mock_which, mock_run):
    """
    Verify is_selinux_enforcing() returns False when `getenforce` returns 'Permissive'.
    """
    mock_run.return_value = MagicMock(stdout="Permissive", returncode=0)
    assert not os_detector.is_selinux_enforcing()


@patch("subprocess.run")
@patch("shutil.which", return_value="/usr/sbin/getenforce")
def test_is_selinux_enforcing_when_disabled(mock_which, mock_run):
    """
    Verify is_selinux_enforcing() returns False when `getenforce` returns 'Disabled'.
    """
    mock_run.return_value = MagicMock(stdout="Disabled", returncode=0)
    assert not os_detector.is_selinux_enforcing()


@patch("subprocess.run", side_effect=subprocess.SubprocessError("Command failed"))
@patch("shutil.which", return_value="/usr/sbin/getenforce")
def test_is_selinux_enforcing_handles_subprocess_error(mock_which, mock_run):
    """
    Verify is_selinux_enforcing() returns False on a SubprocessError.
    """
    assert not os_detector.is_selinux_enforcing()
