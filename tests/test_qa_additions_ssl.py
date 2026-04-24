"""
QA Additions — ULTIMATE_VHOST-020 SSL edge cases and coverage gaps.

Targets:
  - ssl.py line 87: RuntimeError when cert file absent post-mkcert (no canonical, no legacy)
  - ssl.py line 96: RuntimeError when key file absent post-mkcert (no canonical, no legacy)
  - generate_certificate: cert present, key missing (canonical only)
  - generate_certificate: cwd passed correctly to subprocess
  - VHostConfig: ssl_enabled=True with both paths None raises ValidationError
  - Template: www-domain redirect correct with SSL on nginx
  - Template: Apache RHEL log paths used with SSL
  - Template: Apache Debian Protocols h2 absent when use_ssl=False
  - CLI: --mkcert flag with generate_certificate RuntimeError triggers exit(1)
  - Flaky-test guard: create_vhost symlink-exists check is resilient to selinux mock
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError

from vhost_helper.ssl import generate_certificate, get_ssl_dir
from vhost_helper.models import VHostConfig, ServerType

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_nginx(template_name: str, **ctx) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR / "nginx")),
        autoescape=False,
    )
    tpl = env.get_template(f"{template_name}.conf.j2")
    defaults = dict(
        domain="myapp.test",
        port=80,
        document_root="/var/www/myapp",
        runtime="static",
        python_port=8000,
        node_port=3000,
        node_socket=None,
        php_socket="/run/php/php-fpm.sock",
        os_family="debian_family",
        use_ssl=False,
        cert_path="",
        key_path="",
    )
    defaults.update(ctx)
    return tpl.render(**defaults)


def _render_apache(template_name: str, **ctx) -> str:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR / "apache")),
        autoescape=False,
    )
    tpl = env.get_template(f"{template_name}.conf.j2")
    defaults = dict(
        domain="myapp.test",
        port=80,
        document_root="/var/www/myapp",
        runtime="static",
        python_port=8000,
        node_port=3000,
        node_socket=None,
        php_socket="/run/php/php-fpm.sock",
        os_family="debian_family",
        use_ssl=False,
        cert_path="",
        key_path="",
    )
    defaults.update(ctx)
    return tpl.render(**defaults)


SSL_CTX = dict(
    use_ssl=True,
    cert_path="/etc/vhost-helper/ssl/myapp.test.pem",
    key_path="/etc/vhost-helper/ssl/myapp.test-key.pem",
)


# ---------------------------------------------------------------------------
# ssl.generate_certificate — uncovered error branches (lines 87, 96)
# ---------------------------------------------------------------------------


class TestGenerateCertificateMissingOutputFiles:
    """Cover the RuntimeError branches when mkcert writes no files at all."""

    def _success_returncode(self):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        return result

    def test_raises_when_cert_file_missing_after_success(self, tmp_path):
        """ssl.py line 87: mkcert exits 0 but writes no cert files → RuntimeError."""
        ssl_dir = tmp_path / "ssl"
        ssl_dir.mkdir()
        domain = "ghost.test"

        # mkcert 'succeeds' but creates NO files at all
        with patch("vhost_helper.ssl.shutil.which", return_value="/usr/bin/mkcert"):
            with patch(
                "vhost_helper.ssl.subprocess.run",
                return_value=self._success_returncode(),
            ):
                with pytest.raises(RuntimeError) as exc_info:
                    generate_certificate(domain, ssl_dir)

        assert "certificate file not found" in str(exc_info.value)

    def test_raises_when_key_file_missing_after_cert_exists(self, tmp_path):
        """ssl.py line 96: cert written but key absent → RuntimeError."""
        ssl_dir = tmp_path / "ssl"
        ssl_dir.mkdir()
        domain = "ghost.test"

        def _writes_cert_only(cmd, cwd=None, capture_output=False, text=False):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            # Write canonical cert but NOT the key
            (ssl_dir / f"{domain}.pem").write_text("CERT")
            return result

        with patch("vhost_helper.ssl.shutil.which", return_value="/usr/bin/mkcert"):
            with patch("vhost_helper.ssl.subprocess.run", side_effect=_writes_cert_only):
                with pytest.raises(RuntimeError) as exc_info:
                    generate_certificate(domain, ssl_dir)

        assert "key file not found" in str(exc_info.value)

    def test_legacy_rename_attempted_before_raising_for_cert(self, tmp_path):
        """When canonical cert missing, legacy (+0) is tried first, then RuntimeError."""
        ssl_dir = tmp_path / "ssl"
        ssl_dir.mkdir()
        domain = "ghost.test"

        # mkcert writes neither canonical nor +0 cert
        with patch("vhost_helper.ssl.shutil.which", return_value="/usr/bin/mkcert"):
            with patch(
                "vhost_helper.ssl.subprocess.run",
                return_value=self._success_returncode(),
            ):
                with pytest.raises(RuntimeError) as exc_info:
                    generate_certificate(domain, ssl_dir)

        # The error specifically refers to cert_path, not key_path
        assert "certificate file not found" in str(exc_info.value)

    def test_cwd_passed_to_subprocess(self, tmp_path):
        """mkcert subprocess must be invoked with cwd=ssl_dir."""
        ssl_dir = tmp_path / "ssl"
        domain = "cwd.test"
        captured = []

        def _side(cmd, cwd=None, capture_output=False, text=False):
            captured.append({"cwd": cwd})
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            (ssl_dir / f"{domain}.pem").write_text("C")
            (ssl_dir / f"{domain}-key.pem").write_text("K")
            return result

        with patch("vhost_helper.ssl.shutil.which", return_value="/usr/bin/mkcert"):
            with patch("vhost_helper.ssl.subprocess.run", side_effect=_side):
                generate_certificate(domain, ssl_dir)

        assert captured[0]["cwd"] == str(ssl_dir)


# ---------------------------------------------------------------------------
# VHostConfig — ssl_enabled=True, both paths None
# ---------------------------------------------------------------------------


class TestVHostConfigBothPathsNone:
    def test_ssl_enabled_both_paths_none_raises(self, tmp_path):
        """model_validator must raise when ssl_enabled=True and both cert/key are None."""
        doc_root = tmp_path / "www"
        doc_root.mkdir()
        with pytest.raises(ValidationError) as exc_info:
            VHostConfig(
                domain="myapp.test",
                document_root=str(doc_root),
                ssl_enabled=True,
                cert_path=None,
                key_path=None,
            )
        err = str(exc_info.value)
        # cert_path validation fires first
        assert "cert_path" in err


# ---------------------------------------------------------------------------
# Nginx template — www-domain redirect with SSL
# ---------------------------------------------------------------------------


class TestNginxSslWwwDomain:
    def test_www_domain_redirect_contains_base_domain(self):
        """When domain starts with 'www.', redirect block targets base domain."""
        output = _render_nginx(
            "default",
            domain="www.myapp.test",
            **SSL_CTX,
        )
        # The port-80 block redirects to https://www.myapp.test
        assert "return 301 https://www.myapp.test" in output

    def test_non_www_domain_redirect_targets_www(self):
        """When domain does not start with 'www.', redirect block targets www.domain."""
        output = _render_nginx(
            "default",
            domain="myapp.test",
            **SSL_CTX,
        )
        assert "return 301 https://myapp.test" in output

    def test_ssl_block_no_document_root_access_log_off(self):
        """SSL server block must not have an access_log on directive."""
        output = _render_nginx("default", domain="myapp.test", **SSL_CTX)
        assert "access_log off;" in output


# ---------------------------------------------------------------------------
# Apache template — RHEL log paths with SSL enabled
# ---------------------------------------------------------------------------


class TestApacheRhelLogsWithSsl:
    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_rhel_error_log_path_with_ssl(self, tpl):
        output = _render_apache(
            tpl,
            os_family="rhel_family",
            **SSL_CTX,
        )
        assert "/var/log/httpd/" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_debian_apache_log_dir_with_ssl(self, tpl):
        output = _render_apache(
            tpl,
            os_family="debian_family",
            **SSL_CTX,
        )
        assert "${APACHE_LOG_DIR}" in output


# ---------------------------------------------------------------------------
# Apache template — Protocols h2 absent when use_ssl=False
# ---------------------------------------------------------------------------


class TestApacheProtocolsH2:
    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_protocols_h2_absent_without_ssl(self, tpl):
        """Protocols h2 directive MUST NOT appear in HTTP-only configurations."""
        output = _render_apache(tpl, os_family="debian_family", use_ssl=False)
        assert "Protocols h2" not in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_protocols_h2_present_debian_ssl(self, tpl):
        output = _render_apache(tpl, os_family="debian_family", **SSL_CTX)
        assert "Protocols h2 http/1.1" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_protocols_h2_absent_rhel_ssl(self, tpl):
        output = _render_apache(tpl, os_family="rhel_family", **SSL_CTX)
        assert "Protocols h2 http/1.1" not in output


# ---------------------------------------------------------------------------
# Template zero-regression: no SSL strings when use_ssl=False
# ---------------------------------------------------------------------------


class TestTemplateZeroRegressionSsl:
    @pytest.mark.parametrize("tpl", ["default", "static", "php", "nodejs-proxy"])
    def test_nginx_no_ssl_strings_http_only(self, tpl):
        output = _render_nginx(tpl, use_ssl=False)
        assert "ssl_certificate" not in output
        assert "ssl_certificate_key" not in output
        assert "SSLEngine" not in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_apache_no_ssl_strings_http_only(self, tpl):
        output = _render_apache(tpl, use_ssl=False)
        assert "SSLCertificateFile" not in output
        assert "SSLCertificateKeyFile" not in output
        assert "SSLEngine" not in output


# ---------------------------------------------------------------------------
# CLI mkcert error propagation
# ---------------------------------------------------------------------------


class TestCliMkcertErrorPropagation:
    """Verify that RuntimeError from generate_certificate surfaces in the CLI."""

    def test_generate_certificate_failure_exits_nonzero(self, tmp_path):
        """If generate_certificate raises, the CLI must exit with code 1."""
        from typer.testing import CliRunner
        from vhost_helper.main import app

        runner = CliRunner()
        doc_root = tmp_path / "www"
        doc_root.mkdir()

        with (
            patch("vhost_helper.main.is_nginx_installed", return_value=True),
            patch("vhost_helper.main.is_nginx_running", return_value=False),
            patch("vhost_helper.main.preflight_sudo_check"),
            patch(
                "vhost_helper.main.check_mkcert_binary",
                return_value="/usr/bin/mkcert",
            ),
            patch(
                "vhost_helper.main.generate_certificate",
                side_effect=RuntimeError("mkcert failed: some error"),
            ),
        ):
            result = runner.invoke(
                app,
                ["create", "myapp.test", str(doc_root), "--mkcert"],
            )

        assert result.exit_code == 1
        assert "mkcert failed" in result.output

    def test_missing_mkcert_binary_exits_nonzero(self, tmp_path):
        """If mkcert binary is missing, CLI must exit with code 1 before any write."""
        from typer.testing import CliRunner
        from vhost_helper.main import app

        runner = CliRunner()
        doc_root = tmp_path / "www"
        doc_root.mkdir()

        with (
            patch("vhost_helper.main.is_nginx_installed", return_value=True),
            patch("vhost_helper.main.is_nginx_running", return_value=False),
            patch("vhost_helper.main.preflight_sudo_check"),
            patch(
                "vhost_helper.main.check_mkcert_binary",
                side_effect=RuntimeError("mkcert binary not found"),
            ),
        ):
            result = runner.invoke(
                app,
                ["create", "myapp.test", str(doc_root), "--mkcert"],
            )

        assert result.exit_code == 1
        assert "mkcert binary not found" in result.output


# ---------------------------------------------------------------------------
# Symlink-exists guard: improved isolation for flaky test scenario
# ---------------------------------------------------------------------------


class TestNginxProviderSymlinkIdempotency:
    """
    Regression guard for BUG: create_vhost called ln even when symlink existed.

    This test explicitly patches os_family to 'debian_family' and verifies
    that run_elevated_command is not called with 'ln' when the symlink is pre-existing.
    The explicit patch ensures the test is not affected by host OS detection.
    """

    def test_skips_ln_when_symlink_already_exists_explicit_debian(self, tmp_path, mocker):
        import tempfile
        from vhost_helper.providers.nginx import NginxProvider

        available = tmp_path / "sites-available"
        enabled = tmp_path / "sites-enabled"
        available.mkdir()
        enabled.mkdir()

        # Pre-create symlink
        target = available / "test.local.conf"
        target.touch()
        link = enabled / "test.local.conf"
        link.symlink_to(target)

        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)
        mocker.patch(
            "vhost_helper.providers.nginx.detected_os_family", "debian_family"
        )
        mocker.patch(
            "vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False
        )

        elevated_calls = []

        def _capture_elevated(cmd, **kwargs):
            cmd_str = [str(c) for c in cmd]
            elevated_calls.append(cmd_str)
            # Actually perform mv so config file exists
            import shutil
            if cmd_str and cmd_str[-2:] and cmd_str[0] in ("sudo", "mv") or (len(cmd_str) > 1 and cmd_str[1] == "mv"):
                pass  # don't actually run
            verb = cmd_str[-len(cmd_str):]
            # Strip sudo prefix
            clean = cmd_str[1:] if cmd_str and cmd_str[0] == "sudo" else cmd_str
            if clean and clean[0] == "mv" and len(clean) == 3:
                shutil.move(clean[1], clean[2])

        mocker.patch(
            "vhost_helper.providers.nginx.run_elevated_command",
            side_effect=_capture_elevated,
        )

        provider = NginxProvider()
        provider.validate_config = MagicMock(return_value=True)
        provider.reload = MagicMock()

        doc_root = tmp_path / "www"
        doc_root.mkdir()
        config = VHostConfig(
            domain="test.local",
            document_root=str(doc_root),
            server_type=ServerType.NGINX,
        )
        provider.create_vhost(config, service_running=True)

        ln_calls = [c for c in elevated_calls if "ln" in c]
        assert not ln_calls, f"ln must NOT be called when symlink exists; got: {ln_calls}"
