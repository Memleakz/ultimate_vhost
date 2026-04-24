"""
Tests for ULTIMATE_VHOST-020: Automated Local SSL via mkcert.

Covers:
- ssl.py: check_mkcert_binary, get_ssl_dir, generate_certificate, ensure_ssl_dir
- models.py: VHostConfig SSL field validation
- Template rendering: use_ssl=True/False for all Nginx and Apache templates
- CLI integration: --mkcert flag happy path and missing-binary error path
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError

# Adjust sys.path via conftest.py — vhost_helper is importable directly.
from vhost_helper.ssl import (
    MKCERT_NOT_FOUND_MSG,
    DEFAULT_SSL_DIR,
    SSL_DIR_ENV_VAR,
    check_mkcert_binary,
    ensure_ssl_dir,
    generate_certificate,
    get_ssl_dir,
)
from vhost_helper.models import VHostConfig, ServerType, RuntimeMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


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


# ---------------------------------------------------------------------------
# ssl.get_ssl_dir
# ---------------------------------------------------------------------------


class TestGetSslDir:
    def test_cli_option_takes_precedence_over_env(self, monkeypatch):
        monkeypatch.setenv(SSL_DIR_ENV_VAR, "/env/ssl")
        result = get_ssl_dir("/cli/ssl")
        assert result == Path("/cli/ssl")

    def test_env_var_used_when_no_cli_option(self, monkeypatch):
        monkeypatch.setenv(SSL_DIR_ENV_VAR, "/env/ssl")
        result = get_ssl_dir(None)
        assert result == Path("/env/ssl")

    def test_default_when_no_cli_or_env(self, monkeypatch):
        monkeypatch.delenv(SSL_DIR_ENV_VAR, raising=False)
        result = get_ssl_dir(None)
        assert result == Path(DEFAULT_SSL_DIR)


# ---------------------------------------------------------------------------
# ssl.check_mkcert_binary
# ---------------------------------------------------------------------------


class TestCheckMkcertBinary:
    def test_returns_path_when_binary_present(self):
        with patch("vhost_helper.ssl.shutil.which", return_value="/usr/bin/mkcert"):
            result = check_mkcert_binary()
        assert result == "/usr/bin/mkcert"

    def test_raises_runtime_error_when_binary_absent(self):
        with patch("vhost_helper.ssl.shutil.which", return_value=None):
            with pytest.raises(RuntimeError) as exc_info:
                check_mkcert_binary()
        assert "mkcert binary not found" in str(exc_info.value)

    def test_error_message_contains_debian_hint(self):
        with patch("vhost_helper.ssl.shutil.which", return_value=None):
            with pytest.raises(RuntimeError) as exc_info:
                check_mkcert_binary()
        assert "apt install mkcert" in str(exc_info.value)

    def test_error_message_contains_upstream_url(self):
        with patch("vhost_helper.ssl.shutil.which", return_value=None):
            with pytest.raises(RuntimeError) as exc_info:
                check_mkcert_binary()
        assert "github.com/FiloSottile/mkcert" in str(exc_info.value)

    def test_exact_not_found_phrase(self):
        """PRD F2: error output MUST contain the exact phrase 'mkcert binary not found'."""
        with patch("vhost_helper.ssl.shutil.which", return_value=None):
            with pytest.raises(RuntimeError) as exc_info:
                check_mkcert_binary()
        assert "mkcert binary not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ssl.ensure_ssl_dir
# ---------------------------------------------------------------------------


class TestEnsureSslDir:
    def test_creates_directory_with_mode_0750(self, tmp_path):
        target = tmp_path / "ssl"
        assert not target.exists()
        ensure_ssl_dir(target)
        assert target.exists()
        assert target.is_dir()
        assert oct(target.stat().st_mode)[-3:] == "750"

    def test_idempotent_when_dir_exists(self, tmp_path):
        target = tmp_path / "ssl"
        target.mkdir()
        ensure_ssl_dir(target)  # must not raise
        assert target.exists()


# ---------------------------------------------------------------------------
# ssl.generate_certificate
# ---------------------------------------------------------------------------


class TestGenerateCertificate:
    def _make_mkcert_side_effect(self, ssl_dir: Path, domain: str):
        """Return a side-effect that creates the canonical cert files."""

        def _side_effect(cmd, cwd=None, capture_output=False, text=False):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            # Simulate mkcert writing <domain>.pem and <domain>-key.pem
            (ssl_dir / f"{domain}.pem").write_text("CERT")
            (ssl_dir / f"{domain}-key.pem").write_text("KEY")
            return result

        return _side_effect

    def test_returns_cert_and_key_paths(self, tmp_path):
        domain = "myapp.test"
        ssl_dir = tmp_path / "ssl"
        with patch("vhost_helper.ssl.shutil.which", return_value="/usr/bin/mkcert"):
            with patch(
                "vhost_helper.ssl.subprocess.run",
                side_effect=self._make_mkcert_side_effect(ssl_dir, domain),
            ):
                cert, key = generate_certificate(domain, ssl_dir)

        assert cert == ssl_dir / "myapp.test.pem"
        assert key == ssl_dir / "myapp.test-key.pem"
        assert cert.exists()
        assert key.exists()

    def test_subprocess_called_with_shell_false(self, tmp_path):
        """PRD F7: mkcert MUST be invoked with shell=False (list-form arguments)."""
        domain = "myapp.test"
        ssl_dir = tmp_path / "ssl"
        captured_calls = []

        def _side_effect(cmd, cwd=None, capture_output=False, text=False):
            captured_calls.append({"cmd": cmd, "cwd": cwd})
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            (ssl_dir / f"{domain}.pem").write_text("CERT")
            (ssl_dir / f"{domain}-key.pem").write_text("KEY")
            return result

        with patch("vhost_helper.ssl.shutil.which", return_value="/usr/bin/mkcert"):
            with patch("vhost_helper.ssl.subprocess.run", side_effect=_side_effect):
                generate_certificate(domain, ssl_dir)

        assert len(captured_calls) == 1
        assert isinstance(captured_calls[0]["cmd"], list)
        assert captured_calls[0]["cmd"] == ["/usr/bin/mkcert", "myapp.test"]

    def test_cert_files_have_mode_0640(self, tmp_path):
        domain = "myapp.test"
        ssl_dir = tmp_path / "ssl"

        with patch("vhost_helper.ssl.shutil.which", return_value="/usr/bin/mkcert"):
            with patch(
                "vhost_helper.ssl.subprocess.run",
                side_effect=self._make_mkcert_side_effect(ssl_dir, domain),
            ):
                cert, key = generate_certificate(domain, ssl_dir)

        assert oct(cert.stat().st_mode)[-3:] == "640"
        assert oct(key.stat().st_mode)[-3:] == "640"

    def test_raises_when_mkcert_not_found(self, tmp_path):
        with patch("vhost_helper.ssl.shutil.which", return_value=None):
            with pytest.raises(RuntimeError) as exc_info:
                generate_certificate("myapp.test", tmp_path / "ssl")
        assert "mkcert binary not found" in str(exc_info.value)

    def test_raises_when_mkcert_exits_nonzero(self, tmp_path):
        domain = "myapp.test"
        ssl_dir = tmp_path / "ssl"
        ssl_dir.mkdir()

        failed_result = MagicMock()
        failed_result.returncode = 1
        failed_result.stderr = "some mkcert error"

        with patch("vhost_helper.ssl.shutil.which", return_value="/usr/bin/mkcert"):
            with patch("vhost_helper.ssl.subprocess.run", return_value=failed_result):
                with pytest.raises(RuntimeError) as exc_info:
                    generate_certificate(domain, ssl_dir)
        assert "mkcert failed" in str(exc_info.value)

    def test_renames_legacy_cert_name(self, tmp_path):
        """When mkcert writes <domain>+0.pem, the file is renamed to <domain>.pem."""
        domain = "myapp.test"
        ssl_dir = tmp_path / "ssl"

        def _side_effect(cmd, cwd=None, capture_output=False, text=False):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            # Write legacy filenames
            (ssl_dir / f"{domain}+0.pem").write_text("CERT")
            (ssl_dir / f"{domain}+0-key.pem").write_text("KEY")
            return result

        with patch("vhost_helper.ssl.shutil.which", return_value="/usr/bin/mkcert"):
            with patch("vhost_helper.ssl.subprocess.run", side_effect=_side_effect):
                cert, key = generate_certificate(domain, ssl_dir)

        assert cert.name == "myapp.test.pem"
        assert key.name == "myapp.test-key.pem"


# ---------------------------------------------------------------------------
# models.VHostConfig — SSL field validation
# ---------------------------------------------------------------------------


class TestVHostConfigSslFields:
    def test_ssl_disabled_by_default(self, tmp_path):
        doc_root = tmp_path / "www"
        doc_root.mkdir()
        config = VHostConfig(domain="myapp.test", document_root=str(doc_root))
        assert config.ssl_enabled is False
        assert config.cert_path is None
        assert config.key_path is None

    def test_ssl_enabled_with_paths_is_valid(self, tmp_path):
        doc_root = tmp_path / "www"
        doc_root.mkdir()
        config = VHostConfig(
            domain="myapp.test",
            document_root=str(doc_root),
            ssl_enabled=True,
            cert_path="/etc/vhost-helper/ssl/myapp.test.pem",
            key_path="/etc/vhost-helper/ssl/myapp.test-key.pem",
        )
        assert config.ssl_enabled is True
        assert config.cert_path == Path("/etc/vhost-helper/ssl/myapp.test.pem")
        assert config.key_path == Path("/etc/vhost-helper/ssl/myapp.test-key.pem")

    def test_ssl_enabled_without_cert_path_raises(self, tmp_path):
        doc_root = tmp_path / "www"
        doc_root.mkdir()
        with pytest.raises(ValidationError) as exc_info:
            VHostConfig(
                domain="myapp.test",
                document_root=str(doc_root),
                ssl_enabled=True,
                cert_path=None,
                key_path="/etc/vhost-helper/ssl/myapp.test-key.pem",
            )
        assert "cert_path" in str(exc_info.value)

    def test_ssl_enabled_without_key_path_raises(self, tmp_path):
        doc_root = tmp_path / "www"
        doc_root.mkdir()
        with pytest.raises(ValidationError) as exc_info:
            VHostConfig(
                domain="myapp.test",
                document_root=str(doc_root),
                ssl_enabled=True,
                cert_path="/etc/vhost-helper/ssl/myapp.test.pem",
                key_path=None,
            )
        assert "key_path" in str(exc_info.value)

    def test_ssl_disabled_with_no_paths_is_valid(self, tmp_path):
        doc_root = tmp_path / "www"
        doc_root.mkdir()
        config = VHostConfig(
            domain="myapp.test",
            document_root=str(doc_root),
            ssl_enabled=False,
        )
        assert config.ssl_enabled is False


# ---------------------------------------------------------------------------
# Nginx template rendering — use_ssl=False (zero regression)
# ---------------------------------------------------------------------------


class TestNginxTemplateNoSsl:
    @pytest.mark.parametrize("tpl", ["default", "static", "php", "nodejs-proxy"])
    def test_no_443_in_output(self, tpl):
        output = _render_nginx(tpl)
        assert "443" not in output

    @pytest.mark.parametrize("tpl", ["default", "static", "php", "nodejs-proxy"])
    def test_no_ssl_certificate_in_output(self, tpl):
        output = _render_nginx(tpl)
        assert "ssl_certificate" not in output

    @pytest.mark.parametrize("tpl", ["default", "static", "php", "nodejs-proxy"])
    def test_listens_on_port_80(self, tpl):
        output = _render_nginx(tpl, port=80)
        assert "listen 80" in output


# ---------------------------------------------------------------------------
# Nginx template rendering — use_ssl=True
# ---------------------------------------------------------------------------


SSL_CTX = dict(
    use_ssl=True,
    cert_path="/etc/vhost-helper/ssl/myapp.test.pem",
    key_path="/etc/vhost-helper/ssl/myapp.test-key.pem",
)


class TestNginxTemplateWithSsl:
    @pytest.mark.parametrize("tpl", ["default", "static", "php", "nodejs-proxy"])
    def test_listen_443_ssl_present(self, tpl):
        output = _render_nginx(tpl, **SSL_CTX)
        assert "listen 443 ssl" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "php", "nodejs-proxy"])
    def test_ssl_certificate_path_present(self, tpl):
        output = _render_nginx(tpl, **SSL_CTX)
        assert "ssl_certificate /etc/vhost-helper/ssl/myapp.test.pem" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "php", "nodejs-proxy"])
    def test_ssl_certificate_key_path_present(self, tpl):
        output = _render_nginx(tpl, **SSL_CTX)
        assert "ssl_certificate_key /etc/vhost-helper/ssl/myapp.test-key.pem" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "php", "nodejs-proxy"])
    def test_http_redirect_to_https_present(self, tpl):
        output = _render_nginx(tpl, **SSL_CTX)
        assert "return 301 https://" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "php", "nodejs-proxy"])
    def test_no_plain_http_80_server_block(self, tpl):
        """With SSL enabled the port-80 block must only redirect, not serve content."""
        output = _render_nginx(tpl, **SSL_CTX)
        # The 80-listener exists only as a redirect block
        assert "listen 80" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "php", "nodejs-proxy"])
    def test_ipv6_listener_present(self, tpl):
        output = _render_nginx(tpl, **SSL_CTX)
        assert "[::]:443 ssl" in output


# ---------------------------------------------------------------------------
# Apache template rendering — use_ssl=False (zero regression)
# ---------------------------------------------------------------------------


class TestApacheTemplateNoSsl:
    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_no_443_in_output(self, tpl):
        output = _render_apache(tpl)
        assert "443" not in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_no_ssl_engine_in_output(self, tpl):
        output = _render_apache(tpl)
        assert "SSLEngine" not in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_no_ssl_certificate_file_in_output(self, tpl):
        output = _render_apache(tpl)
        assert "SSLCertificateFile" not in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_listens_on_port_80(self, tpl):
        output = _render_apache(tpl, port=80)
        assert "<VirtualHost *:80>" in output


# ---------------------------------------------------------------------------
# Apache template rendering — use_ssl=True
# ---------------------------------------------------------------------------


class TestApacheTemplateWithSsl:
    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_virtualhost_443_present(self, tpl):
        output = _render_apache(tpl, **SSL_CTX)
        assert "<VirtualHost *:443>" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_ssl_engine_on_present(self, tpl):
        output = _render_apache(tpl, **SSL_CTX)
        assert "SSLEngine on" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_ssl_certificate_file_present(self, tpl):
        output = _render_apache(tpl, **SSL_CTX)
        assert "SSLCertificateFile /etc/vhost-helper/ssl/myapp.test.pem" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_ssl_certificate_key_file_present(self, tpl):
        output = _render_apache(tpl, **SSL_CTX)
        assert (
            "SSLCertificateKeyFile /etc/vhost-helper/ssl/myapp.test-key.pem"
            in output
        )

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_http_redirect_block_present(self, tpl):
        output = _render_apache(tpl, **SSL_CTX)
        assert "<VirtualHost *:80>" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_debian_protocols_h2_present(self, tpl):
        output = _render_apache(tpl, os_family="debian_family", **SSL_CTX)
        assert "Protocols h2 http/1.1" in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_rhel_no_protocols_h2(self, tpl):
        output = _render_apache(tpl, os_family="rhel_family", **SSL_CTX)
        assert "Protocols h2 http/1.1" not in output

    @pytest.mark.parametrize("tpl", ["default", "static", "nodejs-proxy"])
    def test_redirect_to_https_in_http_block(self, tpl):
        output = _render_apache(tpl, **SSL_CTX)
        assert "https://" in output


# ---------------------------------------------------------------------------
# CLI integration via NginxProvider (mocked)
# ---------------------------------------------------------------------------


class TestNginxProviderSslIntegration:
    """Verify NginxProvider passes SSL context through to template rendering."""

    def _make_cmd_side_effect(self):
        """Return a side-effect for run_elevated_command that performs real mv/chmod/ln."""
        import shutil as _shutil

        def _side_effect(cmd, **kwargs):
            cmd = [str(c) for c in cmd]
            # Strip any leading 'sudo' prefix
            if cmd and cmd[0] == "sudo":
                cmd = cmd[1:]
            if not cmd:
                return
            verb = cmd[0]
            if verb == "mv" and len(cmd) == 3:
                _shutil.move(cmd[1], cmd[2])
            elif verb == "chmod":
                pass  # ignore permission changes in tests
            elif verb == "ln" and "-s" in cmd:
                src = cmd[-2]
                dst = cmd[-1]
                Path(dst).symlink_to(src)
            # All other elevated commands are silently ignored

        return _side_effect

    def test_provider_renders_ssl_config(self, tmp_path):
        from vhost_helper.providers.nginx import NginxProvider

        doc_root = tmp_path / "www"
        doc_root.mkdir()
        sites_avail = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_avail.mkdir()
        sites_enabled.mkdir()

        config = VHostConfig(
            domain="myapp.test",
            document_root=str(doc_root),
            ssl_enabled=True,
            cert_path="/etc/vhost-helper/ssl/myapp.test.pem",
            key_path="/etc/vhost-helper/ssl/myapp.test-key.pem",
        )

        with (
            patch(
                "vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_avail
            ),
            patch(
                "vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled
            ),
            patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None),
            patch(
                "vhost_helper.providers.nginx.detected_os_family", "debian_family"
            ),
            patch(
                "vhost_helper.providers.nginx.is_selinux_enforcing",
                return_value=False,
            ),
            patch(
                "vhost_helper.providers.nginx.run_elevated_command",
                side_effect=self._make_cmd_side_effect(),
            ),
        ):
            provider = NginxProvider()
            provider.validate_config = MagicMock(return_value=True)
            provider.reload = MagicMock()
            provider.create_vhost(config, service_running=False)

        # Locate the written config file
        conf_file = sites_avail / "myapp.test.conf"
        assert conf_file.exists()
        content = conf_file.read_text()
        assert "listen 443 ssl" in content
        assert "ssl_certificate /etc/vhost-helper/ssl/myapp.test.pem" in content
        assert "ssl_certificate_key /etc/vhost-helper/ssl/myapp.test-key.pem" in content

    def test_provider_renders_http_only_config_without_ssl(self, tmp_path):
        from vhost_helper.providers.nginx import NginxProvider

        doc_root = tmp_path / "www"
        doc_root.mkdir()
        sites_avail = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_avail.mkdir()
        sites_enabled.mkdir()

        config = VHostConfig(
            domain="myapp.test",
            document_root=str(doc_root),
        )

        with (
            patch(
                "vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_avail
            ),
            patch(
                "vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled
            ),
            patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None),
            patch(
                "vhost_helper.providers.nginx.detected_os_family", "debian_family"
            ),
            patch(
                "vhost_helper.providers.nginx.is_selinux_enforcing",
                return_value=False,
            ),
            patch(
                "vhost_helper.providers.nginx.run_elevated_command",
                side_effect=self._make_cmd_side_effect(),
            ),
        ):
            provider = NginxProvider()
            provider.validate_config = MagicMock(return_value=True)
            provider.reload = MagicMock()
            provider.create_vhost(config, service_running=False)

        conf_file = sites_avail / "myapp.test.conf"
        content = conf_file.read_text()
        assert "443" not in content
        assert "ssl_certificate" not in content


# ---------------------------------------------------------------------------
# CLI integration via ApacheProvider (mocked)
# ---------------------------------------------------------------------------


class TestApacheProviderSslIntegration:
    """Verify ApacheProvider passes SSL context through to template rendering."""

    def _make_cmd_side_effect(self):
        """Return a side-effect for run_elevated_command that performs real mv/chmod/ln."""
        import shutil as _shutil

        def _side_effect(cmd, **kwargs):
            cmd = [str(c) for c in cmd]
            if cmd and cmd[0] == "sudo":
                cmd = cmd[1:]
            if not cmd:
                return
            verb = cmd[0]
            if verb == "mv" and len(cmd) == 3:
                _shutil.move(cmd[1], cmd[2])
            elif verb == "ln" and "-s" in cmd:
                src = cmd[-2]
                dst = cmd[-1]
                Path(dst).symlink_to(src)

        return _side_effect

    def test_provider_renders_ssl_config(self, tmp_path):
        from vhost_helper.providers.apache import ApacheProvider

        doc_root = tmp_path / "www"
        doc_root.mkdir()
        sites_avail = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_avail.mkdir()
        sites_enabled.mkdir()

        config = VHostConfig(
            domain="myapp.test",
            document_root=str(doc_root),
            server_type=ServerType.APACHE,
            ssl_enabled=True,
            cert_path="/etc/vhost-helper/ssl/myapp.test.pem",
            key_path="/etc/vhost-helper/ssl/myapp.test-key.pem",
        )

        with (
            patch(
                "vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", sites_avail
            ),
            patch(
                "vhost_helper.providers.apache.APACHE_SITES_ENABLED", sites_enabled
            ),
            patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", None),
            patch(
                "vhost_helper.providers.apache.detected_os_family", "debian_family"
            ),
            patch(
                "vhost_helper.providers.apache.is_selinux_enforcing",
                return_value=False,
            ),
            patch(
                "vhost_helper.providers.apache.run_elevated_command",
                side_effect=self._make_cmd_side_effect(),
            ),
        ):
            provider = ApacheProvider()
            provider.validate_config = MagicMock(return_value=True)
            provider.reload = MagicMock()
            provider.create_vhost(config, service_running=False)

        conf_file = sites_avail / "myapp.test.conf"
        assert conf_file.exists()
        content = conf_file.read_text()
        assert "<VirtualHost *:443>" in content
        assert "SSLEngine on" in content
        assert "SSLCertificateFile /etc/vhost-helper/ssl/myapp.test.pem" in content
        assert (
            "SSLCertificateKeyFile /etc/vhost-helper/ssl/myapp.test-key.pem"
            in content
        )
