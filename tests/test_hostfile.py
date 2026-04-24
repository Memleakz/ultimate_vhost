import os
import tempfile
import pytest
import subprocess
from vhost_helper.hostfile import add_entry, remove_entry
import vhost_helper.hostfile

@pytest.fixture
def temp_hosts_file():
    with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
        tmp.write("127.0.0.1\tlocalhost\n")
        tmp_path = tmp.name
    
    old_hosts = vhost_helper.hostfile.HOSTS_FILE
    vhost_helper.hostfile.HOSTS_FILE = tmp_path
    yield tmp_path
    vhost_helper.hostfile.HOSTS_FILE = old_hosts
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

def test_add_remove_entry(temp_hosts_file, mocker):
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")

    # Test adding a new entry
    add_entry("127.0.0.1", "test.local")
    assert mock_run.called

    # Test removing an entry
    remove_entry("test.local")
    assert mock_run.called

def test_mock_add_entry(temp_hosts_file, mocker):
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")

    add_entry("127.0.0.1", "test.local")
    assert mock_run.called

def test_mock_remove_entry(temp_hosts_file, mocker):
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")
    # Mock sudo prefix to trigger tee path
    mocker.patch("vhost_helper.hostfile.get_sudo_prefix", return_value=["sudo"])

    # Ensure the entry exists in the temp file
    with open(temp_hosts_file, "a") as f:
        f.write("127.0.0.1\ttest.local\n")

    remove_entry("test.local")
    assert mock_run.called
