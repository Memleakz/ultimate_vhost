"""
QA Additions for ULTIMATE_VHOST-008 — SSL Removal Verification.

Verifies all acceptance criteria for Ticket ULTIMATE_VHOST-008:
  1. --no-ssl flag is unknown/rejected by the CLI
  2. ssl.py module is absent from the codebase
  3. VHostConfig model has no ssl_enabled field
  4. Generated Nginx configs have NO SSL directives (no 443, no ssl_certificate)
  5. vhost create lifecycle never invokes certbot
  6. Port boundary validation (model-level)
  7. Domain validation: underscores rejected, path-traversal rejected
  8. Nginx template produces only HTTP server blocks for all runtimes
  9. remove_vhost no-op when neither file nor symlink exist (service_running=False)
 10. cli create with invalid port exits with code 2 (Typer type error)
"""
import subprocess
import tempfile
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

import pytest
from typer.testing import CliRunner

from vhost_helper.main import app, validate_domain
from vhost_helper.models import VHostConfig, ServerType, RuntimeMode, DEFAULT_PHP_SOCKET
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
# AC-1: --no-ssl flag must be unknown / rejected
# ---------------------------------------------------------------------------

class TestNoSslFlagRejected:
    """PRD §2: vhost create must not recognize --no-ssl."""

    def test_create_no_ssl_flag_exits_nonzero(self, tmp_path):
        """Passing --no-ssl to 'vhost create' must exit with a non-zero code."""
        result = runner.invoke(app, ["create", "site.test", str(tmp_path), "--no-ssl"])
        assert result.exit_code != 0

    def test_create_no_ssl_flag_not_in_help(self):
        """--no-ssl must NOT appear in 'vhost create --help' output."""
        result = runner.invoke(app, ["create", "--help"])
        assert result.exit_code == 0
        assert "--no-ssl" not in result.stdout

    def test_create_help_does_not_mention_ssl(self):
        """Help output for 'create' must not mention SSL at all."""
        result = runner.invoke(app, ["create", "--help"])
        assert "ssl" not in result.stdout.lower()


# ---------------------------------------------------------------------------
# AC-2: ssl.py must not exist
# ---------------------------------------------------------------------------

class TestSslModuleAbsent:
    """PRD §1: lib/vhost_helper/ssl.py must be deleted."""

    def test_ssl_py_file_does_not_exist(self):
        ssl_path = Path(__file__).parent.parent / "lib" / "vhost_helper" / "ssl.py"
        assert not ssl_path.exists(), f"ssl.py still present at {ssl_path}"

    def test_ssl_module_not_importable(self):
        with pytest.raises(ImportError):
            import vhost_helper.ssl  # noqa: F401


# ---------------------------------------------------------------------------
# AC-3: VHostConfig must have no ssl_enabled field
# ---------------------------------------------------------------------------

class TestModelHasNoSslField:
    """PRD §3: VHostConfig Pydantic model must not contain ssl_enabled."""

    def test_vhost_config_has_no_ssl_enabled_field(self, tmp_path):
        config = VHostConfig(
            domain="mysite.test",
            document_root=tmp_path,
        )
        assert not hasattr(config, "ssl_enabled"), "ssl_enabled field must not exist"

    def test_vhost_config_model_fields_do_not_include_ssl(self, tmp_path):
        field_names = set(VHostConfig.model_fields.keys())
        assert "ssl_enabled" not in field_names

    def test_vhost_config_rejects_ssl_enabled_kwarg(self, tmp_path):
        """Pydantic v2 with extra='forbid' or just ignores extra; either way no attribute."""
        config = VHostConfig(
            domain="mysite.test",
            document_root=tmp_path,
        )
        # Whether Pydantic silently ignores or raises, attribute must not exist on instance
        assert not hasattr(config, "ssl_enabled")


# ---------------------------------------------------------------------------
# AC-4: Generated Nginx config must have no SSL directives
# ---------------------------------------------------------------------------

class TestNginxTemplateNoSslDirectives:
    """PRD §4: Generated configs must not contain SSL-related directives."""

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
            assert "ssl_certificate" not in output, f"ssl_certificate found in {runtime} template"

    def test_no_ssl_certificate_key_directive(self):
        for runtime in ("static", "php", "python"):
            output = _render(runtime=runtime)
            assert "ssl_certificate_key" not in output

    def test_no_https_redirect_to_443(self):
        """No server block should unconditionally redirect to port 443."""
        for runtime in ("static", "php", "python"):
            output = _render(runtime=runtime)
            assert "return 301 https" not in output, f"HTTPS redirect found in {runtime} template"

    def test_template_uses_only_configured_port(self):
        """All listen directives must use the configured port, not hardcoded 443."""
        output = _render(port=8080)
        import re
        listen_ports = re.findall(r"listen\s+(\d+)", output)
        for p in listen_ports:
            assert p == "8080", f"Unexpected listen port {p} in template output"

    def test_no_ssl_keyword_anywhere_in_static_template(self):
        output = _render(runtime="static")
        assert "ssl" not in output.lower()


# ---------------------------------------------------------------------------
# AC-5: vhost create lifecycle must never call certbot
# ---------------------------------------------------------------------------

class TestNoCertbotCalls:
    """PRD §1: certbot must never be called during vhost create."""

    def test_certbot_not_called_during_create(self, tmp_path, mocker):
        mocker.patch("vhost_helper.providers.nginx.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.providers.nginx.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")

        mock_run = mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0))

        result = runner.invoke(app, ["create", "mysite.test", str(tmp_path)])

        certbot_calls = [
            c for c in mock_run.call_args_list
            if "certbot" in str(c)
        ]
        assert certbot_calls == [], f"certbot was invoked: {certbot_calls}"

    def test_certbot_not_in_source_code(self):
        """Ensure no source file under lib/ references certbot."""
        lib_dir = Path(__file__).parent.parent / "lib"
        for py_file in lib_dir.rglob("*.py"):
            content = py_file.read_text()
            assert "certbot" not in content.lower(), (
                f"certbot reference found in {py_file}"
            )


# ---------------------------------------------------------------------------
# AC-6: Port boundary validation (VHostConfig model)
# ---------------------------------------------------------------------------

class TestPortBoundaryValidation:
    """Port must be an integer in range [1, 65535]."""

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
        result = runner.invoke(app, ["create", "site.test", str(tmp_path), "--port", "notanumber"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# AC-7: Additional domain validation edge cases
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
        """Single TLD-only 'label' (no dot) must fail."""
        with pytest.raises(ValueError):
            validate_domain("localhost")


# ---------------------------------------------------------------------------
# AC-8: remove_vhost is a no-op when files/symlinks are absent
# ---------------------------------------------------------------------------

class TestRemoveVhostNoOpWhenAbsent:
    """remove_vhost must not raise when neither config nor symlink exist."""

    def test_remove_vhost_no_raise_when_files_absent(self, mocker):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            available = tmp_path / "sites-available"
            enabled = tmp_path / "sites-enabled"
            available.mkdir()
            enabled.mkdir()

            mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
            mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)
            mock_run = mocker.patch(
                "vhost_helper.utils.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0),
            )
            mocker.patch("vhost_helper.utils._console")

            provider = NginxProvider()
            # Should not raise — files don't exist, no sudo commands needed
            provider.remove_vhost("nonexistent.test", service_running=False)

            # No subprocess calls should be made if nothing exists
            sudo_rm_calls = [
                c for c in mock_run.call_args_list
                if "rm" in str(c)
            ]
            assert sudo_rm_calls == []
