"""
Tests for ULTIMATE_VHOST-006: Fix CLI Password Prompt Visibility During Sudo Operations.

Acceptance criteria verified here:
3.1  sys.stdout.flush() and sys.stderr.flush() called before subprocess spawned.
3.2  Prescribed message "[vhost] Elevated privileges required..." printed before subprocess.
3.3  stdin is never subprocess.PIPE or subprocess.DEVNULL on sudo calls;
     ValueError raised if caller tries to pass either.
3.4  RuntimeError raised with command info when subprocess exits non-zero.
     Successful runs return the CompletedProcess without raising.
"""

import subprocess
from unittest.mock import MagicMock

import pytest

from vhost_helper.utils import (
    _ELEVATED_MESSAGE,
    run_elevated_command,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


# ---------------------------------------------------------------------------
# 3.1 — Stdout / Stderr flushing
# ---------------------------------------------------------------------------


def test_stdout_flushed_before_subprocess_for_sudo_command(mocker):
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mock_stdout = mocker.patch("vhost_helper.utils.sys.stdout")
    mock_stderr = mocker.patch("vhost_helper.utils.sys.stderr")

    run_elevated_command(["sudo", "echo", "hello"])

    mock_stdout.flush.assert_called()
    mock_stderr.flush.assert_called()
    # Both flushes must have happened before subprocess.run was called
    assert mock_stdout.flush.call_count >= 1
    assert mock_stderr.flush.call_count >= 1


def test_stdout_flush_called_before_subprocess_spawn(mocker):
    call_order = []
    mock_stdout = MagicMock()
    mock_stdout.flush.side_effect = lambda: call_order.append("flush_stdout")
    mock_stderr = MagicMock()
    mock_stderr.flush.side_effect = lambda: call_order.append("flush_stderr")

    def fake_run(cmd, **kwargs):
        call_order.append("subprocess_run")
        return _make_completed(0)

    mocker.patch("vhost_helper.utils.subprocess.run", side_effect=fake_run)
    mocker.patch("vhost_helper.utils.sys.stdout", mock_stdout)
    mocker.patch("vhost_helper.utils.sys.stderr", mock_stderr)
    mocker.patch("vhost_helper.utils._console")

    run_elevated_command(["sudo", "mv", "/tmp/a", "/tmp/b"])

    subprocess_index = call_order.index("subprocess_run")
    stdout_index = call_order.index("flush_stdout")
    stderr_index = call_order.index("flush_stderr")
    assert stdout_index < subprocess_index, "stdout must be flushed before subprocess"
    assert stderr_index < subprocess_index, "stderr must be flushed before subprocess"


def test_no_flush_when_no_sudo_in_command(mocker):
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mock_stdout = mocker.patch("vhost_helper.utils.sys.stdout")
    mock_stderr = mocker.patch("vhost_helper.utils.sys.stderr")

    run_elevated_command(["echo", "hello"])

    mock_stdout.flush.assert_not_called()
    mock_stderr.flush.assert_not_called()


# ---------------------------------------------------------------------------
# 3.2 — Pre-prompt message
# ---------------------------------------------------------------------------


def test_elevated_message_constant_exact_text():
    assert _ELEVATED_MESSAGE == (
        "[vhost] Elevated privileges required. You may be prompted for your password."
    )


def test_prescribed_message_printed_before_subprocess(mocker):
    call_order = []
    mock_console = MagicMock()
    mock_console.print.side_effect = lambda *a, **kw: call_order.append("message")

    def fake_run(cmd, **kwargs):
        call_order.append("subprocess_run")
        return _make_completed(0)

    mocker.patch("vhost_helper.utils.subprocess.run", side_effect=fake_run)
    mocker.patch("vhost_helper.utils._console", mock_console)
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")

    run_elevated_command(["sudo", "chmod", "644", "/etc/nginx/sites-available/foo"])

    msg_index = call_order.index("message")
    run_index = call_order.index("subprocess_run")
    assert (
        msg_index < run_index
    ), "Prescribed message must appear before subprocess is spawned"


def test_prescribed_message_contains_required_text(mocker):
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mock_console = MagicMock()
    mocker.patch("vhost_helper.utils._console", mock_console)
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")

    run_elevated_command(["sudo", "mv", "/tmp/x", "/tmp/y"])

    printed_args = mock_console.print.call_args_list
    assert len(printed_args) >= 1
    full_text = " ".join(str(a) for args in printed_args for a in args[0])
    assert "[vhost]" in full_text
    assert "Elevated privileges required" in full_text
    assert "password" in full_text.lower()


def test_no_message_printed_without_sudo(mocker):
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
    mock_console = MagicMock()
    mocker.patch("vhost_helper.utils._console", mock_console)

    run_elevated_command(["echo", "hello"])

    mock_console.print.assert_not_called()


# ---------------------------------------------------------------------------
# 3.3 — TTY passthrough (stdin constraints)
# ---------------------------------------------------------------------------


def test_raises_value_error_when_stdin_is_pipe():
    with pytest.raises(ValueError, match="PIPE"):
        run_elevated_command(["sudo", "tee", "-a", "/etc/hosts"], stdin=subprocess.PIPE)


def test_raises_value_error_when_stdin_is_devnull():
    with pytest.raises(ValueError, match="DEVNULL"):
        run_elevated_command(
            ["sudo", "tee", "-a", "/etc/hosts"], stdin=subprocess.DEVNULL
        )


def test_subprocess_called_with_stdin_none_by_default(mocker):
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run", return_value=_make_completed(0)
    )
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch("vhost_helper.utils._console")

    run_elevated_command(["sudo", "rm", "/tmp/foo"])

    _, kwargs = mock_run.call_args
    assert kwargs.get("stdin") is None


def test_subprocess_called_with_file_handle_stdin(mocker, tmp_path):
    mock_run = mocker.patch(
        "vhost_helper.utils.subprocess.run", return_value=_make_completed(0)
    )
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch("vhost_helper.utils._console")

    entry_file = tmp_path / "entry.txt"
    entry_file.write_bytes(b"127.0.0.1\ttest.test\n")

    with open(entry_file, "rb") as fh:
        run_elevated_command(["sudo", "tee", "-a", "/etc/hosts"], stdin=fh)

    _, kwargs = mock_run.call_args
    assert kwargs.get("stdin") is not None
    assert kwargs["stdin"] not in (subprocess.PIPE, subprocess.DEVNULL)


def test_no_pipe_or_devnull_in_any_sudo_call_in_nginx_provider(mocker, tmp_path):
    """
    Integration-level guard: all subprocess calls made by NginxProvider during
    create_vhost must not use stdin=PIPE or stdin=DEVNULL.
    """
    from vhost_helper.providers.nginx import NginxProvider
    from vhost_helper.models import (
        VHostConfig,
        ServerType,
        RuntimeMode,
        DEFAULT_PHP_SOCKET,
    )

    available = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    available.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.utils.get_sudo_prefix", return_value=[])

    observed_stdin_values = []

    def capturing_run(cmd, **kwargs):
        observed_stdin_values.append(kwargs.get("stdin"))
        return _make_completed(0)

    mocker.patch("vhost_helper.utils.subprocess.run", side_effect=capturing_run)

    doc_root = tmp_path / "project"
    doc_root.mkdir()

    config = VHostConfig(
        domain="example.test",
        document_root=doc_root,
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.STATIC,
        php_socket=DEFAULT_PHP_SOCKET,
    )

    provider = NginxProvider()
    provider.create_vhost(config, service_running=False)

    for val in observed_stdin_values:
        assert val not in (
            subprocess.PIPE,
            subprocess.DEVNULL,
        ), f"subprocess.run was called with stdin={val!r}, which blocks TTY passthrough"


# ---------------------------------------------------------------------------
# 3.4 — Post-privilege execution continuity
# ---------------------------------------------------------------------------


def test_returns_completed_process_on_success(mocker):
    expected = _make_completed(0)
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=expected)
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch("vhost_helper.utils._console")

    result = run_elevated_command(["sudo", "systemctl", "reload", "nginx"])

    assert result is expected
    assert result.returncode == 0


def test_raises_runtime_error_on_nonzero_exit(mocker):
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(1))
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch("vhost_helper.utils._console")

    with pytest.raises(RuntimeError):
        run_elevated_command(["sudo", "systemctl", "reload", "nginx"])


def test_runtime_error_message_contains_exit_code(mocker):
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(3))
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch("vhost_helper.utils._console")

    with pytest.raises(RuntimeError, match="3"):
        run_elevated_command(["sudo", "mv", "/tmp/a", "/tmp/b"])


def test_no_raise_when_check_is_false_and_nonzero_exit(mocker):
    mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(1))
    mocker.patch("vhost_helper.utils.sys.stdout")
    mocker.patch("vhost_helper.utils.sys.stderr")
    mocker.patch("vhost_helper.utils._console")

    result = run_elevated_command(["sudo", "echo", "fail"], check=False)
    assert result.returncode == 1


def test_cli_continues_after_successful_elevated_command(mocker, tmp_path):
    """End-to-end: the CLI emits success messages after sudo operations complete."""
    from typer.testing import CliRunner
    from vhost_helper.main import app

    available = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    available.mkdir()
    enabled.mkdir()
    doc_root = tmp_path / "project"
    doc_root.mkdir()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")
    runner = CliRunner()
    result = runner.invoke(app, ["create", "example.test", str(doc_root)])

    assert result.exit_code == 0
    assert "Virtual host" in result.output or "Success" in result.output
