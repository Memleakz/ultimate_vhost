"""
QA Additions for ULTIMATE_VHOST-006 Phase — Comprehensive gap coverage.

Covers:
- get_sudo_prefix() edge cases (root user, no sudo in PATH)
- run_elevated_command() stdin validation for non-sudo commands
- hostfile.add_entry() direct-write path (no sudo prefix)
- NginxProvider.validate_config() OSError path
- NginxProvider.reload() fallback and full-fail paths
- Template rendering: www-prefix domain redirect logic
- Template rendering: custom port in HTTP mode
- os_detector.get_os_info() FileNotFoundError when script missing
- os_detector.get_os_info() arch/rhel/unknown OS family mapping
- validate_domain() boundary and edge cases
- CLI create rollback on NginxProvider failure
- NginxProvider temp file cleanup on mv failure
"""

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vhost_helper.main import app, validate_domain
from vhost_helper.models import (
    DEFAULT_PHP_SOCKET,
    VHostConfig,
    ServerType,
    RuntimeMode,
)
from vhost_helper.providers.nginx import NginxProvider
from vhost_helper.utils import get_sudo_prefix, run_elevated_command

runner = CliRunner()

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cp(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


def _render(
    domain: str = "example.test",
    document_root: str = "/var/www/example",
    port: int = 80,
    runtime: str = "static",
    python_port: int = 8000,
    php_socket: str = DEFAULT_PHP_SOCKET,
    template_name: str = "default",
) -> str:
    """Renders a template using the provider's logic."""
    provider = NginxProvider()
    template = provider._get_template(template_name)
    return template.render(
        domain=domain,
        document_root=document_root,
        port=port,
        runtime=runtime,
        python_port=python_port,
        php_socket=php_socket,
    )


# ---------------------------------------------------------------------------
# get_sudo_prefix() edge cases
# ---------------------------------------------------------------------------


class TestGetSudoPrefix:
    def test_returns_empty_when_running_as_root(self, mocker):
        mocker.patch("vhost_helper.utils.os.getuid", return_value=0)
        result = get_sudo_prefix()
        assert result == []

    def test_returns_sudo_when_not_root_and_sudo_on_path(self, mocker):
        mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
        mocker.patch("vhost_helper.utils.shutil.which", return_value="/usr/bin/sudo")
        result = get_sudo_prefix()
        assert result == ["sudo"]

    def test_returns_empty_when_not_root_and_no_sudo(self, mocker):
        mocker.patch("vhost_helper.utils.os.getuid", return_value=1000)
        mocker.patch("vhost_helper.utils.shutil.which", return_value=None)
        result = get_sudo_prefix()
        assert result == []


# ---------------------------------------------------------------------------
# run_elevated_command() — stdin PIPE/DEVNULL blocked for any call
# ---------------------------------------------------------------------------


class TestRunElevatedCommandStdinValidation:
    def test_pipe_blocked_on_non_sudo_command(self):
        """stdin=PIPE must be rejected even without sudo in the cmd."""
        with pytest.raises(ValueError, match="PIPE"):
            run_elevated_command(["echo", "hello"], stdin=subprocess.PIPE)

    def test_devnull_blocked_on_non_sudo_command(self):
        """stdin=DEVNULL must be rejected even without sudo in the cmd."""
        with pytest.raises(ValueError, match="DEVNULL"):
            run_elevated_command(["echo", "hello"], stdin=subprocess.DEVNULL)

    def test_no_flush_or_message_when_no_sudo(self, mocker):
        """Without 'sudo' in cmd, flushing and message printing are skipped."""
        mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_cp(0))
        mock_stdout = mocker.patch("vhost_helper.utils.sys.stdout")
        mock_console = mocker.patch("vhost_helper.utils._console")

        run_elevated_command(["ls", "/tmp"])

        mock_stdout.flush.assert_not_called()
        mock_console.print.assert_not_called()


# ---------------------------------------------------------------------------
# hostfile.add_entry() — direct write path (no sudo prefix / root user)
# ---------------------------------------------------------------------------


class TestHostfileAddEntryDirectWrite:
    def test_add_entry_writes_directly_when_no_sudo(self, mocker, tmp_path):
        """When get_sudo_prefix returns [], entry is written via direct file I/O."""
        hosts_file = tmp_path / "hosts"
        hosts_file.write_text("127.0.0.1\tlocalhost\n")

        import vhost_helper.hostfile as hf

        old = hf.HOSTS_FILE
        hf.HOSTS_FILE = str(hosts_file)

        mocker.patch("vhost_helper.hostfile.get_sudo_prefix", return_value=[])
        mock_run = mocker.patch("vhost_helper.utils.subprocess.run")

        try:
            hf.add_entry("127.0.0.1", "direct.test")
        finally:
            hf.HOSTS_FILE = old

        # subprocess.run should NOT have been called (used direct write)
        mock_run.assert_not_called()
        assert "direct.test" in hosts_file.read_text()

    def test_add_entry_skips_when_entry_already_exists_same_ip(self, mocker, tmp_path):
        """Duplicate detection prevents a second write."""
        hosts_file = tmp_path / "hosts"
        hosts_file.write_text("127.0.0.1\texisting.test\n")

        import vhost_helper.hostfile as hf

        old = hf.HOSTS_FILE
        hf.HOSTS_FILE = str(hosts_file)
        mock_run = mocker.patch("vhost_helper.utils.subprocess.run")

        try:
            hf.add_entry("127.0.0.1", "existing.test")
        finally:
            hf.HOSTS_FILE = old

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# NginxProvider.validate_config() — OSError path
# ---------------------------------------------------------------------------


class TestNginxValidateConfigEdgeCases:
    def test_returns_false_on_file_not_found(self, mocker):
        """FileNotFoundError from subprocess must be caught and return False."""
        mocker.patch(
            "vhost_helper.utils.subprocess.run",
            side_effect=FileNotFoundError("nginx not on PATH"),
        )
        provider = NginxProvider()
        assert provider.validate_config() is False

    def test_returns_false_on_os_error(self, mocker):
        """OSError from subprocess must be caught and return False."""
        mocker.patch(
            "vhost_helper.utils.subprocess.run",
            side_effect=OSError("permission denied"),
        )
        provider = NginxProvider()
        assert provider.validate_config() is False

    def test_returns_false_on_runtime_error(self, mocker):
        """Non-zero exit (RuntimeError from run_elevated_command) returns False."""
        mocker.patch(
            "vhost_helper.utils.subprocess.run",
            return_value=_make_cp(1),
        )
        provider = NginxProvider()
        assert provider.validate_config() is False


# ---------------------------------------------------------------------------
# NginxProvider.reload() — fallback and double-fail paths
# ---------------------------------------------------------------------------


class TestNginxReload:
    def test_fallback_nginx_s_reload_when_systemctl_fails(self, mocker):
        """When systemctl reload fails, nginx -s reload is attempted as fallback."""
        call_count = [0]

        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            if "systemctl" in cmd:
                return _make_cp(1)  # systemctl fails
            return _make_cp(0)  # nginx -s reload succeeds

        mocker.patch("vhost_helper.utils.subprocess.run", side_effect=side_effect)
        mocker.patch("vhost_helper.utils._console")
        mocker.patch("vhost_helper.utils.sys.stdout")
        mocker.patch("vhost_helper.utils.sys.stderr")
        mocker.patch("vhost_helper.utils.get_sudo_prefix", return_value=[])

        provider = NginxProvider()
        # Should not raise
        provider.reload()
        assert call_count[0] >= 2

    def test_raises_runtime_error_when_both_reload_methods_fail(self, mocker):
        """Both systemctl and nginx -s reload failing must raise RuntimeError."""
        mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_cp(1))
        mocker.patch("vhost_helper.utils._console")
        mocker.patch("vhost_helper.utils.sys.stdout")
        mocker.patch("vhost_helper.utils.sys.stderr")
        mocker.patch("vhost_helper.utils.get_sudo_prefix", return_value=[])

        provider = NginxProvider()
        with pytest.raises(RuntimeError, match="Failed to reload Nginx"):
            provider.reload()


# ---------------------------------------------------------------------------
# NginxProvider.create_vhost() — temp file cleanup on failure
# ---------------------------------------------------------------------------


class TestNginxCreateVhostTempCleanup:
    def test_temp_file_removed_on_mv_failure(self, mocker, tmp_path):
        """If 'mv' command fails, the temp file is cleaned up."""
        available = tmp_path / "sites-available"
        enabled = tmp_path / "sites-enabled"
        available.mkdir()
        enabled.mkdir()

        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)
        mocker.patch("vhost_helper.utils.get_sudo_prefix", return_value=[])

        # Make subprocess.run fail on mv
        mocker.patch("vhost_helper.utils.subprocess.run", return_value=_make_cp(1))
        mocker.patch("vhost_helper.utils._console")

        doc_root = tmp_path / "project"
        doc_root.mkdir()

        config = VHostConfig(
            domain="cleanup.test",
            document_root=doc_root,
            server_type=ServerType.NGINX,
            runtime=RuntimeMode.STATIC,
            php_socket=DEFAULT_PHP_SOCKET,
        )

        provider = NginxProvider()
        with pytest.raises(RuntimeError):
            provider.create_vhost(config, service_running=False)

        # No temp .conf files should remain
        remaining = list(tmp_path.glob("*.conf"))
        assert remaining == [], f"Temp file not cleaned up: {remaining}"


# ---------------------------------------------------------------------------
# Template rendering — www-prefix domain redirect logic
# ---------------------------------------------------------------------------


class TestTemplateWwwRedirect:
    def test_www_domain_redirects_to_non_www(self):
        """Input www.example.test → redirect_domain is example.test."""
        rendered = _render(domain="www.example.test")
        assert "server_name example.test" in rendered

    def test_non_www_domain_redirects_to_www(self):
        """Input example.test → redirect_domain is www.example.test."""
        rendered = _render(domain="example.test")
        assert "server_name www.example.test" in rendered

    def test_redirect_uses_301(self):
        """Canonical redirect must be a 301 (permanent) not 302."""
        rendered = _render(domain="mysite.test")
        assert "return 301" in rendered
        assert "302" not in rendered

    def test_http_mode_uses_custom_port(self):
        """HTTP mode must honour the port variable."""
        rendered = _render(domain="custom.test", port=8080)
        assert "listen 8080" in rendered
        assert "listen 443" not in rendered


# ---------------------------------------------------------------------------
# os_detector.get_os_info() — edge cases
# ---------------------------------------------------------------------------


class TestOsDetectorEdgeCases:
    def test_raises_file_not_found_when_script_missing(self, mocker):
        """If detect_os.sh is not found, FileNotFoundError is raised."""
        mocker.patch("vhost_helper.os_detector.Path.exists", return_value=False)
        from vhost_helper.os_detector import get_os_info

        with pytest.raises(FileNotFoundError, match="OS detection script not found"):
            get_os_info()

    def test_arch_family_correctly_mapped(self, mocker):
        """ID=arch must map to family='arch'."""
        mocker.patch("vhost_helper.os_detector.Path.exists", return_value=True)
        mocker.patch(
            "vhost_helper.os_detector.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ID=arch\nVERSION=rolling\n"
            ),
        )
        from vhost_helper.os_detector import get_os_info

        info = get_os_info()
        assert info.family == "arch"
        assert info.id == "arch"

    def test_fedora_family_correctly_mapped(self, mocker):
        """ID=fedora must map to family='rhel'."""
        mocker.patch("vhost_helper.os_detector.Path.exists", return_value=True)
        mocker.patch(
            "vhost_helper.os_detector.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ID=fedora\nVERSION=39\n"
            ),
        )
        from vhost_helper.os_detector import get_os_info

        info = get_os_info()
        assert info.family == "rhel"

    def test_unknown_os_id_returns_unknown_family(self, mocker):
        """Unrecognised OS ID must produce family='unknown'."""
        mocker.patch("vhost_helper.os_detector.Path.exists", return_value=True)
        mocker.patch(
            "vhost_helper.os_detector.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="ID=gentoo\nVERSION=2.14\n"
            ),
        )
        from vhost_helper.os_detector import get_os_info

        info = get_os_info()
        assert info.family == "unknown"


# ---------------------------------------------------------------------------
# validate_domain() — boundary and edge cases
# ---------------------------------------------------------------------------


class TestValidateDomainEdgeCases:
    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            validate_domain("")

    def test_domain_exceeding_253_chars_raises_value_error(self):
        # 254 chars: 'a' * 251 + '.ab'
        long_domain = "a" * 251 + ".ab"
        assert len(long_domain) > 253
        with pytest.raises(ValueError, match="too long"):
            validate_domain(long_domain)

    def test_domain_with_double_dot_raises_value_error(self):
        with pytest.raises(ValueError, match="double dots"):
            validate_domain("example..test")

    def test_valid_short_domain_accepted(self):
        """A 5-character domain (a.bc) must pass validation."""
        result = validate_domain("a.bc")
        assert result == "a.bc"

    def test_valid_domain_with_hyphens_accepted(self):
        result = validate_domain("my-site.test")
        assert result == "my-site.test"

    def test_domain_starting_with_hyphen_rejected(self):
        with pytest.raises(ValueError, match="Invalid domain format"):
            validate_domain("-bad.test")

    def test_domain_ending_with_hyphen_rejected(self):
        # The string itself ends with a hyphen — unambiguously invalid.
        with pytest.raises(ValueError, match="Invalid domain format"):
            validate_domain("test.dom-")

    def test_domain_label_ending_with_hyphen_rejected(self):
        # "bad-.test" has a label "bad-" that ends with a hyphen, which is
        # invalid per RFC 1035. The validator must reject this.
        with pytest.raises(ValueError, match="Invalid domain format"):
            validate_domain("bad-.test")

    def test_domain_with_special_chars_rejected(self):
        with pytest.raises(ValueError, match="Invalid domain format"):
            validate_domain("bad_domain.test")


# ---------------------------------------------------------------------------
# CLI create — rollback when NginxProvider.create_vhost raises
# ---------------------------------------------------------------------------


class TestCliCreateRollback:
    def test_rollback_removes_hostfile_entry_on_nginx_failure(self, mocker, tmp_path):
        """If NginxProvider.create_vhost raises, the hostfile entry is rolled back."""
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
        mocker.patch("vhost_helper.main.preflight_sudo_check")

        mock_add = mocker.patch("vhost_helper.main.add_entry")
        mock_remove = mocker.patch("vhost_helper.main.remove_entry")
        mocker.patch(
            "vhost_helper.providers.nginx.NginxProvider.create_vhost",
            side_effect=RuntimeError("config write failed"),
        )

        result = runner.invoke(app, ["create", "rollback.test", str(doc_root)])

        assert result.exit_code == 1
        # add_entry is called for both the domain and its www counterpart
        assert mock_add.call_count == 2
        # remove_entry is called for both domain and www counterpart in rollback
        assert mock_remove.call_count == 2
        mock_remove.assert_any_call("rollback.test")

    def test_exit_code_1_when_nginx_not_installed(self, mocker, tmp_path):
        """Exit code 1 and clear message when no web server binary is missing."""
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=False)
        mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)
        doc_root = tmp_path / "project"
        doc_root.mkdir()

        result = runner.invoke(app, ["create", "example.test", str(doc_root)])
        assert result.exit_code == 1
        assert "No supported web server found" in result.output

    def test_php_and_python_flags_mutually_exclusive(self, mocker, tmp_path):
        """--php and --python together must exit 1 with mutual-exclusion message."""
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        doc_root = tmp_path / "project"
        doc_root.mkdir()

        result = runner.invoke(
            app, ["create", "example.test", str(doc_root), "--php", "__auto__", "--python"]
        )
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output
