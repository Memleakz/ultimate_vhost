"""
Tests for ULTIMATE_VHOST-007: Fix Suppressed Sudo Password Prompts in CLI Execution.

Acceptance criteria verified here:
3.1  preflight_sudo_check() runs sudo -v before any spinner is started.
     - Root UID returns immediately (no-op).
     - No sudo binary returns immediately (no-op).
     - TTY + successful sudo -v warms cache without raising.
     - sudo -v failure raises SystemExit(1) with human-readable error.
3.2  run_elevated_command() stops the active spinner before spawning subprocess.
     - Spinner .stop() is called before subprocess.run().
     - _active_live is cleared (set to None) after stopping.
     - Spinner is not restarted inside run_elevated_command.
3.3  run_elevated_command() prints "Privileges confirmed." after a successful sudo command.
     - Confirmation is NOT printed on failure (non-zero exit code).
     - Confirmation is NOT printed for non-sudo commands.
3.4  Non-TTY guard: plain-text warning to stderr, operation continues.
     - Warning text matches the constant.
     - Tool does not abort when stdin is not a TTY.
"""
import subprocess
import sys
from io import StringIO
from unittest.mock import MagicMock, call, patch

import pytest

from vhost_helper.utils import (
    _ELEVATED_MESSAGE,
    _NON_TTY_WARNING,
    preflight_sudo_check,
    run_elevated_command,
    set_active_live,
    _active_live,
)
import vhost_helper.utils as utils_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


# ---------------------------------------------------------------------------
# 3.1 — Pre-flight sudo cache warm-up
# ---------------------------------------------------------------------------

def test_preflight_returns_immediately_when_root(mocker):
    """Root user (UID 0) should skip sudo -v entirely."""
    mocker.patch("vhost_helper.utils.os.getuid", return_value=0)
    mock_run = mocker.patch("vhost_helper.utils.subprocess.run")

    preflight_sudo_check()

    mock_run.assert_not_called()


def test_preflight_returns_immediately_when_no_sudo_binary(mocker):
    """No sudo binary → skip silently (Docker / root-only envs)."""
    mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
    mocker.patch("vhost_helper.utils.shutil.which", return_value=None)
    mock_run = mocker.patch("vhost_helper.utils.subprocess.run")

    preflight_sudo_check()

    mock_run.assert_not_called()


def test_preflight_runs_sudo_v_on_interactive_tty(mocker):
    """On an interactive TTY, preflight must call sudo -v."""
    mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
    mocker.patch("vhost_helper.utils.shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("vhost_helper.utils.sys.stdin.isatty", return_value=True)
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run", return_value=_make_completed(0)
    )

    preflight_sudo_check()

    mock_run.assert_called_once_with(["sudo", "-v"])


def test_preflight_does_not_raise_on_successful_sudo_v(mocker):
    """Successful sudo -v (returncode=0) must not raise any exception."""
    mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
    mocker.patch("vhost_helper.utils.shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("vhost_helper.utils.sys.stdin.isatty", return_value=True)
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch(
        "vhost_helper.utils.subprocess.run", return_value=_make_completed(0)
    )

    preflight_sudo_check()  # must not raise


def test_preflight_raises_system_exit_on_sudo_v_failure(mocker):
    """sudo -v failure (returncode!=0) must raise SystemExit(1)."""
    mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
    mocker.patch("vhost_helper.utils.shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("vhost_helper.utils.sys.stdin.isatty", return_value=True)
    mocker.patch("vhost_helper.utils.sys.stdout")
    mock_stderr = mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch(
        "vhost_helper.utils.subprocess.run", return_value=_make_completed(1)
    )

    with pytest.raises(SystemExit) as exc_info:
        preflight_sudo_check()

    assert exc_info.value.code == 1


def test_preflight_failure_message_contains_abort(mocker, capsys):
    """The error message on failure must mention 'sudo privileges' and 'Aborting'."""
    mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
    mocker.patch("vhost_helper.utils.shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("vhost_helper.utils.sys.stdin.isatty", return_value=True)
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(1))

    # Capture real stderr
    buf = StringIO()
    with patch("vhost_helper.utils.sys.stderr", buf):
        with pytest.raises(SystemExit):
            preflight_sudo_check()

    output = buf.getvalue()
    assert "sudo privileges" in output.lower() or "Failed to acquire" in output
    assert "Aborting" in output


def test_preflight_called_before_any_spinner_in_create(mocker, tmp_path):
    """preflight_sudo_check must be called before any console.status() spinner in create."""
    from typer.testing import CliRunner
    from vhost_helper.main import app

    available = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    available.mkdir()
    enabled.mkdir()
    doc_root = tmp_path / "project"
    doc_root.mkdir()

    call_order = []

    def fake_preflight():
        call_order.append("preflight")

    def fake_tracked_status(*args, **kwargs):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            call_order.append("spinner_start")
            yield MagicMock()
            call_order.append("spinner_end")

        return _cm()

    mocker.patch("vhost_helper.main.preflight_sudo_check", side_effect=fake_preflight)
    mocker.patch("vhost_helper.main._tracked_status", side_effect=fake_tracked_status)
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")
    runner = CliRunner()
    runner.invoke(app, ["create", "example.test", str(doc_root)])

    assert "preflight" in call_order, "preflight_sudo_check was never called"
    if "spinner_start" in call_order:
        assert call_order.index("preflight") < call_order.index("spinner_start"), (
            "preflight must run before the first spinner"
        )


def test_preflight_called_before_any_spinner_in_remove(mocker, tmp_path):
    """preflight_sudo_check must be called before any spinner in remove."""
    from typer.testing import CliRunner
    from vhost_helper.main import app

    call_order = []

    def fake_preflight():
        call_order.append("preflight")

    def fake_tracked_status(*args, **kwargs):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            call_order.append("spinner_start")
            yield MagicMock()

        return _cm()

    mocker.patch("vhost_helper.main.preflight_sudo_check", side_effect=fake_preflight)
    mocker.patch("vhost_helper.main._tracked_status", side_effect=fake_tracked_status)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.remove_vhost")

    runner = CliRunner()
    runner.invoke(app, ["remove", "--force", "example.test"])

    assert "preflight" in call_order, "preflight_sudo_check was never called"
    if "spinner_start" in call_order:
        assert call_order.index("preflight") < call_order.index("spinner_start")


# ---------------------------------------------------------------------------
# 3.2 — Spinner suspension before privileged subprocess
# ---------------------------------------------------------------------------

def test_spinner_stopped_before_subprocess_run(mocker):
    """Active spinner must be stopped before subprocess.run() is called."""
    call_order = []

    mock_live = MagicMock()
    mock_live.stop.side_effect = lambda: call_order.append("spinner_stop")

    def fake_run(cmd, **kwargs):
        call_order.append("subprocess_run")
        return _make_completed(0)

    mocker.patch("vhost_helper.utils.subprocess.run", side_effect=fake_run)
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch("vhost_helper.utils._console")

    set_active_live(mock_live)
    try:
        run_elevated_command(["sudo", "mv", "/tmp/a", "/tmp/b"])
    finally:
        set_active_live(None)

    assert "spinner_stop" in call_order, "Spinner .stop() was never called"
    assert "subprocess_run" in call_order
    assert call_order.index("spinner_stop") < call_order.index("subprocess_run"), (
        "Spinner must be stopped before subprocess is spawned"
    )


def test_active_live_cleared_after_spinner_stop(mocker):
    """_active_live must be set to None after the spinner is stopped."""
    mock_live = MagicMock()

    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch("vhost_helper.utils._console")

    set_active_live(mock_live)
    run_elevated_command(["sudo", "echo", "test"])

    assert utils_module._active_live is None, (
        "_active_live must be None after run_elevated_command stops the spinner"
    )


def test_spinner_not_restarted_inside_run_elevated_command(mocker):
    """run_elevated_command must NOT restart the spinner after the subprocess exits."""
    mock_live = MagicMock()
    mock_live.stop.return_value = None
    mock_live.start.return_value = None

    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch("vhost_helper.utils._console")

    set_active_live(mock_live)
    run_elevated_command(["sudo", "chmod", "644", "/etc/nginx/nginx.conf"])
    set_active_live(None)

    mock_live.start.assert_not_called()


def test_no_spinner_stop_when_no_live_registered(mocker):
    """run_elevated_command must not fail when no spinner is registered."""
    set_active_live(None)

    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch("vhost_helper.utils._console")

    run_elevated_command(["sudo", "echo", "no spinner"])  # must not raise


def test_spinner_not_stopped_for_non_sudo_command(mocker):
    """Non-sudo commands must not touch the spinner at all."""
    mock_live = MagicMock()

    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mocker.patch("vhost_helper.utils._console")

    set_active_live(mock_live)
    try:
        run_elevated_command(["echo", "hello"])
    finally:
        set_active_live(None)

    mock_live.stop.assert_not_called()


# ---------------------------------------------------------------------------
# 3.3 — Post-authentication confirmation
# ---------------------------------------------------------------------------

def test_confirmation_printed_after_successful_sudo_command(mocker):
    """✔ Privileges confirmed. must be printed after a successful sudo call."""
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mock_console = MagicMock()
    mocker.patch("vhost_helper.utils._console", mock_console)

    run_elevated_command(["sudo", "mv", "/tmp/a", "/tmp/b"])

    all_printed = " ".join(
        str(a) for c in mock_console.print.call_args_list for a in c[0]
    )
    assert "Privileges confirmed" in all_printed


def test_confirmation_not_printed_on_failure(mocker):
    """✔ Privileges confirmed. must NOT be printed when the sudo command fails."""
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(1))
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mock_console = MagicMock()
    mocker.patch("vhost_helper.utils._console", mock_console)

    with pytest.raises(RuntimeError):
        run_elevated_command(["sudo", "mv", "/tmp/a", "/tmp/b"])

    all_printed = " ".join(
        str(a) for c in mock_console.print.call_args_list for a in c[0]
    )
    assert "Privileges confirmed" not in all_printed


def test_confirmation_not_printed_for_non_sudo_command(mocker):
    """Confirmation must not appear for non-elevated commands."""
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mock_console = MagicMock()
    mocker.patch("vhost_helper.utils._console", mock_console)

    run_elevated_command(["echo", "hello"])

    mock_console.print.assert_not_called()


def test_confirmation_printed_exactly_once_per_sudo_invocation(mocker):
    """Confirmation appears exactly once per run_elevated_command call."""
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mock_console = MagicMock()
    mocker.patch("vhost_helper.utils._console", mock_console)

    run_elevated_command(["sudo", "chmod", "644", "/etc/hosts"])

    confirmation_calls = [
        c for c in mock_console.print.call_args_list
        if "Privileges confirmed" in str(c)
    ]
    assert len(confirmation_calls) == 1, (
        f"Expected exactly 1 confirmation print, got {len(confirmation_calls)}"
    )


def test_confirmation_uses_green_checkmark(mocker):
    """Confirmation line must use a green checkmark (visually distinct from errors)."""
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mock_console = MagicMock()
    mocker.patch("vhost_helper.utils._console", mock_console)

    run_elevated_command(["sudo", "ln", "-s", "/a", "/b"])

    all_printed = " ".join(
        str(a) for c in mock_console.print.call_args_list for a in c[0]
    )
    assert "green" in all_printed
    assert "✔" in all_printed or "Privileges confirmed" in all_printed


# ---------------------------------------------------------------------------
# 3.4 — Non-TTY / CI environment guard
# ---------------------------------------------------------------------------

def test_non_tty_warning_written_to_stderr(mocker):
    """When stdin is not a TTY, a plain-text warning must be written to stderr."""
    mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
    mocker.patch("vhost_helper.utils.shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("vhost_helper.utils.sys.stdin.isatty", return_value=False)
    mock_run = mocker.patch("vhost_helper.utils.subprocess.run")

    buf = StringIO()
    with patch("vhost_helper.utils.sys.stderr", buf):
        preflight_sudo_check()

    output = buf.getvalue()
    assert "Non-interactive terminal" in output or "non-interactive" in output.lower()
    assert "passwordless sudo" in output.lower() or "NOPASSWD" in output


def test_non_tty_warning_does_not_abort(mocker):
    """On non-TTY, preflight_sudo_check must return normally (not raise)."""
    mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
    mocker.patch("vhost_helper.utils.shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("vhost_helper.utils.sys.stdin.isatty", return_value=False)
    mocker.patch("vhost_helper.utils.subprocess.run")

    buf = StringIO()
    with patch("vhost_helper.utils.sys.stderr", buf):
        preflight_sudo_check()  # must not raise


def test_non_tty_warning_is_plain_text_not_rich_markup(mocker):
    """Non-TTY warning must be plain text (no Rich markup like [bold] or [red])."""
    mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
    mocker.patch("vhost_helper.utils.shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("vhost_helper.utils.sys.stdin.isatty", return_value=False)
    mocker.patch("vhost_helper.utils.subprocess.run")

    buf = StringIO()
    with patch("vhost_helper.utils.sys.stderr", buf):
        preflight_sudo_check()

    output = buf.getvalue()
    assert "[bold]" not in output
    assert "[red]" not in output
    assert "[green]" not in output


def test_non_tty_sudo_v_not_called(mocker):
    """In a non-TTY environment, sudo -v must NOT be called (would hang)."""
    mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
    mocker.patch("vhost_helper.utils.shutil.which", return_value="/usr/bin/sudo")
    mocker.patch("vhost_helper.utils.sys.stdin.isatty", return_value=False)
    mock_run = mocker.patch("vhost_helper.utils.subprocess.run")

    buf = StringIO()
    with patch("vhost_helper.utils.sys.stderr", buf):
        preflight_sudo_check()

    mock_run.assert_not_called()


def test_non_tty_constant_text_matches():
    """_NON_TTY_WARNING constant must contain expected key phrases."""
    assert "Non-interactive terminal" in _NON_TTY_WARNING
    assert "passwordless sudo" in _NON_TTY_WARNING.lower() or "NOPASSWD" in _NON_TTY_WARNING


# ---------------------------------------------------------------------------
# Integration: set_active_live / _tracked_status in main
# ---------------------------------------------------------------------------

def test_set_active_live_registers_live_object():
    """set_active_live should update the module-level _active_live."""
    mock_live = MagicMock()
    set_active_live(mock_live)
    assert utils_module._active_live is mock_live
    set_active_live(None)  # cleanup
    assert utils_module._active_live is None


def test_tracked_status_registers_and_clears_live(mocker):
    """_tracked_status context manager must register the live object and clear it on exit."""
    from vhost_helper.main import _tracked_status

    registered = []

    original_set = set_active_live

    def capturing_set(live):
        registered.append(live)
        original_set(live)

    mocker.patch("vhost_helper.main.set_active_live", side_effect=capturing_set)

    with _tracked_status("[bold green]Test spinner[/bold green]", spinner="dots"):
        pass

    # First call registers the Status object (not None), second clears it
    assert len(registered) >= 2
    assert registered[-1] is None, "set_active_live(None) must be called on exit"
