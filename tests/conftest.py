import sys
from pathlib import Path

import pytest

# Add src/lib/ to sys.path so that `vhost_helper` is importable in tests
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))


@pytest.fixture(autouse=True)
def _auto_mock_permissions(request, mocker):
    """
    Automatically mock the permission and SELinux functions that call privileged
    commands (sudo chown / chmod / semanage / chcon) in all tests that do NOT
    explicitly opt-out.

    Tests inside test_permissions.py bypass this fixture because they exercise
    those functions directly and supply their own targeted mocks.
    """
    if "test_permissions" in request.fspath.basename:
        # Let test_permissions.py manage its own mocks
        yield
        return

    # Mock low-level utility functions globally to prevent sudo prompts
    import subprocess

    mocker.patch("vhost_helper.utils.preflight_sudo_check")
    mocker.patch("vhost_helper.utils.get_sudo_prefix", return_value=[])
    mocker.patch(
        "vhost_helper.utils.run_elevated_command",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )

    # Mock high-level functions in main.py to avoid executing side effects
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch("vhost_helper.main.apply_webroot_permissions")
    mocker.patch("vhost_helper.main.apply_selinux_webroot_context")
    mocker.patch("vhost_helper.main.is_selinux_active", return_value=False)
    mocker.patch("vhost_helper.main.start_service", return_value=None)
    mocker.patch("vhost_helper.main.check_mkcert_binary")
    mocker.patch(
        "vhost_helper.main.generate_certificate",
        return_value=("/tmp/cert.pem", "/tmp/key.pem"),
    )
    mocker.patch(
        "vhost_helper.main.get_os_info",
        return_value=type("OSInfo", (), {"family": "debian_family"})(),
    )
    yield
