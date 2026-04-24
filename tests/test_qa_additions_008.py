"""
QA Additions for ULTIMATE_VHOST-008 — SSL Removal Verification (Legacy).

Note: Tests that asserted SSL was *absent* have been removed now that
ULTIMATE_VHOST-020 has implemented mkcert SSL support. Remaining tests
cover port validation, domain validation, and provider no-op behaviour.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vhost_helper.main import app, validate_domain
from vhost_helper.models import VHostConfig, DEFAULT_PHP_SOCKET
from vhost_helper.providers.nginx import NginxProvider

runner = CliRunner()

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render(
    domain: str = "example.test",
    document_root: str = "/var/www/example",
    port: int = 80,
    runtime: str = "static",
    python_port: int = 8000,
    php_socket: str = DEFAULT_PHP_SOCKET,
    template_name: str = "default",
) -> str:
    """Renders a template using the provider's logic (use_ssl=False → HTTP-only)."""
    provider = NginxProvider()
    template = provider._get_template(template_name)
    return template.render(
        domain=domain,
        document_root=document_root,
        port=port,
        runtime=runtime,
        python_port=python_port,
        php_socket=php_socket,
        use_ssl=False,
        cert_path="",
        key_path="",
    )


# ---------------------------------------------------------------------------
# --no-ssl is still unknown (the SSL flag is --mkcert / --no-mkcert)
# ---------------------------------------------------------------------------


class TestNoSslFlagRejected:
    """vhost create must not recognize a standalone --no-ssl flag."""

    def test_create_no_ssl_flag_exits_nonzero(self, tmp_path):
        """Passing --no-ssl (not --no-mkcert) to 'vhost create' must exit non-zero."""
        result = runner.invoke(app, ["create", "site.test", str(tmp_path), "--no-ssl"])
        assert result.exit_code != 0

    def test_create_no_ssl_flag_not_in_help(self):
        """--no-ssl must NOT appear in 'vhost create --help' (the flag is --no-mkcert)."""
        result = runner.invoke(app, ["create", "--help"])
        assert result.exit_code == 0
        assert "--no-ssl" not in result.stdout


# ---------------------------------------------------------------------------
# Generated Nginx config has no SSL directives when use_ssl=False
# ---------------------------------------------------------------------------


class TestNginxTemplateNoSslDirectives:
    """Generated configs must not contain SSL-related directives when use_ssl=False."""

    def test_static_runtime_no_listen_443(self):
        output = _render(runtime="static")
        assert "listen 443" not in output

    def test_php_runtime_no_listen_443(self):
        output = _render(runtime="php")
        assert "listen 443" not in output

    def test_python_runtime_no_listen_443(self):
        output = _render(runtime="python")
        assert "listen 443" not in output

    def test_no_ssl_certificate_directive(self):
        for runtime in ("static", "php", "python"):
            output = _render(runtime=runtime)
            assert (
                "ssl_certificate" not in output
            ), f"ssl_certificate found in {runtime} template"

    def test_no_ssl_certificate_key_directive(self):
        for runtime in ("static", "php", "python"):
            output = _render(runtime=runtime)
            assert "ssl_certificate_key" not in output

    def test_no_https_redirect_to_443(self):
        for runtime in ("static", "php", "python"):
            output = _render(runtime=runtime)
            assert (
                "return 301 https" not in output
            ), f"HTTPS redirect found in {runtime} template"

    def test_template_uses_only_configured_port(self):
        output = _render(port=8080)
        import re

        listen_ports = re.findall(r"listen\s+(\d+)", output)
        for p in listen_ports:
            assert p == "8080", f"Unexpected listen port {p} in template output"

    def test_no_ssl_keyword_anywhere_in_static_template(self):
        output = _render(runtime="static")
        assert "ssl" not in output.lower()


# ---------------------------------------------------------------------------
# vhost create lifecycle must never call certbot
# ---------------------------------------------------------------------------


class TestNoCertbotCalls:
    def test_certbot_not_called_during_create(self, tmp_path, mocker):
        mocker.patch(
            "vhost_helper.providers.nginx.is_nginx_installed", return_value=True
        )
        mocker.patch(
            "vhost_helper.providers.nginx.is_nginx_running", return_value=False
        )
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")

        mock_run = mocker.patch(
            "subprocess.run", return_value=subprocess.CompletedProcess([], 0)
        )

        runner.invoke(app, ["create", "mysite.test", str(tmp_path)])

        certbot_calls = [c for c in mock_run.call_args_list if "certbot" in str(c)]
        assert certbot_calls == [], f"certbot was invoked: {certbot_calls}"

    def test_certbot_not_in_source_code(self):
        lib_dir = Path(__file__).parent.parent / "lib"
        for py_file in lib_dir.rglob("*.py"):
            content = py_file.read_text()
            assert (
                "certbot" not in content.lower()
            ), f"certbot reference found in {py_file}"


# ---------------------------------------------------------------------------
# Port boundary validation (VHostConfig model)
# ---------------------------------------------------------------------------


class TestPortBoundaryValidation:
    def test_port_zero_is_invalid(self, tmp_path):
        with pytest.raises(Exception):
            VHostConfig(domain="site.test", document_root=tmp_path, port=0)

    def test_port_65536_is_invalid(self, tmp_path):
        with pytest.raises(Exception):
            VHostConfig(domain="site.test", document_root=tmp_path, port=65536)

    def test_port_1_is_valid(self, tmp_path):
        config = VHostConfig(domain="site.test", document_root=tmp_path, port=1)
        assert config.port == 1

    def test_port_65535_is_valid(self, tmp_path):
        config = VHostConfig(domain="site.test", document_root=tmp_path, port=65535)
        assert config.port == 65535

    def test_cli_create_with_string_port_exits_nonzero(self, tmp_path):
        result = runner.invoke(
            app, ["create", "site.test", str(tmp_path), "--port", "notanumber"]
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Domain validation edge cases
# ---------------------------------------------------------------------------


class TestDomainValidationEdgeCasesQA008:
    def test_underscore_in_domain_rejected(self):
        with pytest.raises(ValueError):
            validate_domain("my_site.test")

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError):
            validate_domain("../etc/passwd")

    def test_null_byte_in_domain_rejected(self):
        with pytest.raises(ValueError):
            validate_domain("site\x00.test")

    def test_domain_with_at_symbol_rejected(self):
        with pytest.raises(ValueError):
            validate_domain("user@site.test")

    def test_ip_address_without_dot_separator_path_rejected(self):
        with pytest.raises(ValueError):
            validate_domain("localhost")


# ---------------------------------------------------------------------------
# remove_vhost no-op when neither file nor symlink exist
# ---------------------------------------------------------------------------


class TestRemoveVhostNoOpWhenAbsent:
    def test_remove_vhost_no_raise_when_files_absent(self, mocker):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            available = tmp_path / "sites-available"
            enabled = tmp_path / "sites-enabled"
            available.mkdir()
            enabled.mkdir()

            mocker.patch(
                "vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available
            )
            mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)
            mock_run = mocker.patch(
                "vhost_helper.utils.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0),
            )
            mocker.patch("vhost_helper.utils._console")

            provider = NginxProvider()
            provider.remove_vhost("nonexistent.test", service_running=False)

            sudo_rm_calls = [c for c in mock_run.call_args_list if "rm" in str(c)]
            assert sudo_rm_calls == []
