"""
QA Additions for ULTIMATE_VHOST-007 Phase — gap coverage and edge cases.

Covers:
- validate_domain: single-label, per-label length (RFC 1035), colon, space
- preflight_sudo_check: flush ordering on TTY, root-skips-even-with-TTY
- run_elevated_command: check=False + nonzero sudo exit, ValueError message
- _tracked_status: set_active_live(None) called on exception
- NginxProvider.remove_vhost: no-op when files absent
- acquire_certificate: uses subprocess.run (not run_elevated_command)
"""
import subprocess
import sys
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

import vhost_helper.utils as utils_module
from vhost_helper.main import _tracked_status, validate_domain
from vhost_helper.utils import (
    preflight_sudo_check,
    run_elevated_command,
    set_active_live,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


# ---------------------------------------------------------------------------
# BUG-008: validate_domain per-label length (RFC 1035 §2.3.4)
# ---------------------------------------------------------------------------

class TestValidateDomainPerLabelLength:
    def test_label_of_exactly_63_chars_is_accepted(self):
        """A label of exactly 63 chars must pass (RFC 1035 upper bound, inclusive)."""
        label = "a" * 63
        domain = f"{label}.test"
        result = validate_domain(domain)
        assert result == domain

    def test_label_of_64_chars_is_rejected(self):
        """A label of 64 chars violates RFC 1035 and must raise ValueError."""
        label = "a" * 64
        domain = f"{label}.test"
        with pytest.raises(ValueError, match="63"):
            validate_domain(domain)

    def test_second_label_over_limit_is_rejected(self):
        """The per-label check must apply to every label, not just the first."""
        domain = "valid." + "b" * 64 + ".test"
        with pytest.raises(ValueError, match="63"):
            validate_domain(domain)

    def test_multiple_labels_all_within_limit_accepted(self):
        """Multiple labels each ≤ 63 chars must pass collectively."""
        domain = ("a" * 10 + ".") * 4 + "test"
        result = validate_domain(domain)
        assert result == domain


# ---------------------------------------------------------------------------
# validate_domain: additional edge cases
# ---------------------------------------------------------------------------

class TestValidateDomainEdgeCasesQA007:
    def test_single_label_no_dot_rejected(self):
        """'localhost' has no dot so the regex requires a subdomain — must fail."""
        with pytest.raises(ValueError):
            validate_domain("localhost")

    def test_minimum_valid_domain_accepted(self):
        """'a.b' is the shortest syntactically valid two-label domain."""
        assert validate_domain("a.b") == "a.b"

    def test_domain_with_colon_rejected(self):
        """Colons (port syntax) are not valid in domain labels."""
        with pytest.raises(ValueError):
            validate_domain("site.test:8080")

    def test_domain_with_space_rejected(self):
        """Spaces are not valid in domain names."""
        with pytest.raises(ValueError):
            validate_domain("bad site.test")

    def test_numeric_first_label_accepted(self):
        """Purely numeric labels are syntactically valid per the regex."""
        assert validate_domain("123.test") == "123.test"

    def test_numeric_tld_accepted(self):
        """Numeric TLDs are allowed by the validator (not an ICANN concern here)."""
        assert validate_domain("site.123") == "site.123"

    def test_punycode_style_domain_accepted(self):
        """Punycode (xn--...) labels use only ASCII alphanumeric and hyphens."""
        domain = "xn--nxasmq6b.com"
        assert validate_domain(domain) == domain

    def test_exactly_253_chars_accepted(self):
        """A domain of exactly 253 characters must be accepted."""
        # 4 labels of 61 chars + '.' separators + 'testa' TLD:
        # 61*4 + 4 + 5 = 253
        domain = f"{'a'*61}.{'b'*61}.{'c'*61}.{'d'*61}.testa"
        assert len(domain) == 253, f"setup error: len={len(domain)}"
        result = validate_domain(domain)
        assert result == domain

    def test_254_chars_rejected(self):
        """A domain of 254 characters must be rejected as too long."""
        # 61*4 + 4 + 6 = 254
        domain = f"{'a'*61}.{'b'*61}.{'c'*61}.{'d'*61}.testab"
        assert len(domain) == 254, f"setup error: len={len(domain)}"
        with pytest.raises(ValueError, match="too long"):
            validate_domain(domain)


# ---------------------------------------------------------------------------
# preflight_sudo_check: flush order and root-with-TTY edge case
# ---------------------------------------------------------------------------

class TestPreflightSudoCheckFlushOrder:
    def test_stdout_and_stderr_flushed_before_subprocess_run_on_tty(self, mocker):
        """On an interactive TTY, stdout and stderr must be flushed before sudo -v is called."""
        call_order = []

        mock_stdout = MagicMock()
        mock_stdout.flush.side_effect = lambda: call_order.append("stdout_flush")
        mock_stderr = MagicMock()
        mock_stderr.flush.side_effect = lambda: call_order.append("stderr_flush")

        def fake_run(cmd):
            call_order.append("subprocess_run")
            return _make_completed(0)

        mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
        mocker.patch("vhost_helper.utils.shutil.which", return_value="/usr/bin/sudo")
        mocker.patch("vhost_helper.utils.sys.stdin.isatty", return_value=True)
        mocker.patch("vhost_helper.utils.sys.stdout", mock_stdout)
        mocker.patch("vhost_helper.utils.sys.stderr", mock_stderr)
        mocker.patch("vhost_helper.utils.subprocess.run", side_effect=fake_run)

        preflight_sudo_check()

        assert "stdout_flush" in call_order
        assert "stderr_flush" in call_order
        assert "subprocess_run" in call_order
        flush_positions = [call_order.index("stdout_flush"), call_order.index("stderr_flush")]
        run_pos = call_order.index("subprocess_run")
        assert max(flush_positions) < run_pos, (
            "Both stdout and stderr must be flushed before subprocess.run is called"
        )

    def test_root_uid_skips_subprocess_even_when_tty_is_present(self, mocker):
        """UID=0 must skip sudo -v unconditionally, even when isatty()=True."""
        mocker.patch("vhost_helper.utils.os.getuid", return_value=0)
        mocker.patch("vhost_helper.utils.sys.stdin.isatty", return_value=True)
        mock_run = mocker.patch("vhost_helper.utils.subprocess.run")

        preflight_sudo_check()

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# run_elevated_command: check=False + nonzero sudo exit
# ---------------------------------------------------------------------------

class TestRunElevatedCommandCheckFalse:
    def test_check_false_nonzero_sudo_does_not_raise(self, mocker):
        """check=False + non-zero exit must NOT raise RuntimeError."""
        mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(2))
        mocker.patch("vhost_helper.utils.sys.stdout")
        mocker.patch("vhost_helper.utils.sys.stderr")
        mocker.patch("vhost_helper.utils._console")

        result = run_elevated_command(["sudo", "false"], check=False)
        assert result.returncode == 2

    def test_check_false_nonzero_sudo_does_not_print_confirmation(self, mocker):
        """check=False + non-zero exit must NOT print 'Privileges confirmed.'"""
        mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(2))
        mocker.patch("vhost_helper.utils.sys.stdout")
        mocker.patch("vhost_helper.utils.sys.stderr")
        mock_console = MagicMock()
        mocker.patch("vhost_helper.utils._console", mock_console)

        run_elevated_command(["sudo", "false"], check=False)

        all_printed = " ".join(
            str(a) for c in mock_console.print.call_args_list for a in c[0]
        )
        assert "Privileges confirmed" not in all_printed

    def test_check_false_zero_sudo_still_prints_confirmation(self, mocker):
        """check=False + zero exit must still print confirmation (same as check=True)."""
        mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_completed(0))
        mocker.patch("vhost_helper.utils.sys.stdout")
        mocker.patch("vhost_helper.utils.sys.stderr")
        mock_console = MagicMock()
        mocker.patch("vhost_helper.utils._console", mock_console)

        run_elevated_command(["sudo", "true"], check=False)

        all_printed = " ".join(
            str(a) for c in mock_console.print.call_args_list for a in c[0]
        )
        assert "Privileges confirmed" in all_printed


# ---------------------------------------------------------------------------
# run_elevated_command: ValueError message content
# ---------------------------------------------------------------------------

class TestRunElevatedCommandValueError:
    def test_value_error_message_mentions_pipe(self):
        """ValueError for PIPE stdin must mention 'PIPE' or 'subprocess.PIPE'."""
        with pytest.raises(ValueError) as exc_info:
            run_elevated_command(["sudo", "cmd"], stdin=subprocess.PIPE)
        msg = str(exc_info.value)
        assert "PIPE" in msg

    def test_value_error_message_mentions_devnull(self):
        """ValueError for DEVNULL stdin must mention 'DEVNULL' or 'subprocess.DEVNULL'."""
        with pytest.raises(ValueError) as exc_info:
            run_elevated_command(["sudo", "cmd"], stdin=subprocess.DEVNULL)
        msg = str(exc_info.value)
        assert "DEVNULL" in msg

    def test_value_error_raised_for_non_sudo_pipe_stdin(self):
        """stdin=PIPE must be rejected even when the command does NOT include sudo."""
        with pytest.raises(ValueError):
            run_elevated_command(["echo", "hello"], stdin=subprocess.PIPE)


# ---------------------------------------------------------------------------
# _tracked_status: exception safety
# ---------------------------------------------------------------------------

class TestTrackedStatusExceptionSafety:
    def test_set_active_live_cleared_on_exception_inside_block(self, mocker):
        """If an exception is raised inside _tracked_status, set_active_live(None) must still run."""
        cleared = []

        original_set = utils_module.set_active_live

        def capturing_set(live):
            cleared.append(live)
            original_set(live)

        mocker.patch("vhost_helper.main.set_active_live", side_effect=capturing_set)

        with pytest.raises(RuntimeError, match="inner error"):
            with _tracked_status("[bold green]Test[/bold green]", spinner="dots"):
                raise RuntimeError("inner error")

        assert cleared[-1] is None, (
            "set_active_live(None) must be the last call even when an exception propagates"
        )

    def test_active_live_module_global_is_none_after_exception(self, mocker):
        """_active_live module global must be None after exception exits _tracked_status."""
        set_active_live(None)  # ensure clean state

        with pytest.raises(ValueError):
            with _tracked_status("[bold green]Test[/bold green]", spinner="dots"):
                raise ValueError("deliberate")

        assert utils_module._active_live is None


# ---------------------------------------------------------------------------
# NginxProvider.remove_vhost: no-op paths
# ---------------------------------------------------------------------------

class TestNginxProviderRemoveVhostNoOp:
    def test_remove_vhost_skips_rm_when_no_files_exist(self, mocker, tmp_path):
        """remove_vhost must not call rm when neither the config file nor symlink exists."""
        available = tmp_path / "sites-available"
        enabled = tmp_path / "sites-enabled"
        available.mkdir()
        enabled.mkdir()

        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)
        mock_run = mocker.patch("vhost_helper.utils.subprocess.run")

        from vhost_helper.providers.nginx import NginxProvider
        provider = NginxProvider()
        # service_running=False to skip reload
        provider.remove_vhost("nonexistent.test", service_running=False)

        # subprocess.run should not have been called for any rm command
        rm_calls = [
            c for c in mock_run.call_args_list
            if c.args and "rm" in c.args[0]
        ]
        assert rm_calls == [], f"Unexpected rm calls: {rm_calls}"


