"""CLI integration tests for ULTIMATE_VHOST-021 PHP-FPM Auto-linking.

Covers:
- vhost create with --php 8.2 (explicit version, mocked socket + service)
- vhost create with --php (auto-detect, mocked socket + service)
- vhost create with --php 7.4 absent version → exit code 1
- Service start failure → warning shown, vhost still created, exit code 0
- Template rendering: fastcgi_pass/SetHandler present with php_socket set
- Template rendering: fastcgi_pass/SetHandler absent when php_socket is None
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from vhost_helper.main import app
from vhost_helper.models import VHostConfig, RuntimeMode, ServerType
from vhost_helper.providers.nginx import NginxProvider
from vhost_helper.providers.apache import ApacheProvider

runner = CliRunner()

NGINX_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "nginx"
APACHE_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "apache"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_nginx(template_name: str, php_socket=None, **kwargs) -> str:
    provider = NginxProvider()
    tmpl = provider._get_template(template_name)
    ctx = dict(
        domain="example.test",
        document_root="/var/www/example",
        port=80,
        runtime=kwargs.get("runtime", "static"),
        python_port=8000,
        node_port=3000,
        node_socket=None,
        php_socket=php_socket,
        os_family="debian_family",
        use_ssl=False,
        cert_path="",
        key_path="",
    )
    ctx.update(kwargs)
    return tmpl.render(**ctx)


def _render_apache(template_name: str, php_socket=None, **kwargs) -> str:
    provider = ApacheProvider()
    tmpl = provider._get_template(template_name)
    ctx = dict(
        domain="example.test",
        document_root="/var/www/example",
        port=80,
        runtime=kwargs.get("runtime", "static"),
        python_port=8000,
        node_port=3000,
        node_socket=None,
        php_socket=php_socket,
        os_family="debian_family",
        use_ssl=False,
        cert_path="",
        key_path="",
    )
    ctx.update(kwargs)
    return tmpl.render(**ctx)


def _make_doc_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# CLI — happy path with explicit version
# ---------------------------------------------------------------------------


class TestCreateWithExplicitPhpVersion:
    def test_explicit_version_generates_correct_socket_path(self, mocker, tmp_path):
        """--php 8.2 produces a config with the PHP 8.2 socket path."""
        doc_root = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")

        mocker.patch(
            "vhost_helper.main.validate_version_present",
            return_value="/run/php/php8.2-fpm.sock",
        )
        mocker.patch("vhost_helper.main.start_service", return_value=None)

        mock_provider = MagicMock()
        mocker.patch("vhost_helper.main._get_provider", return_value=mock_provider)

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc_root), "--php", "8.2"],
        )

        assert result.exit_code == 0
        call_args = mock_provider.create_vhost.call_args[0]
        config: VHostConfig = call_args[0]
        assert config.php_socket == "/run/php/php8.2-fpm.sock"
        assert config.runtime == RuntimeMode.PHP

    def test_explicit_version_calls_validate_version_present(self, mocker, tmp_path):
        doc_root = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")
        mock_validate = mocker.patch(
            "vhost_helper.main.validate_version_present",
            return_value="/run/php/php8.2-fpm.sock",
        )
        mocker.patch("vhost_helper.main.start_service", return_value=None)
        mocker.patch("vhost_helper.main._get_provider", return_value=MagicMock())

        runner.invoke(
            app,
            ["create", "example.test", str(doc_root), "--php", "8.2"],
        )
        mock_validate.assert_called_once()
        args = mock_validate.call_args[0]
        assert args[0] == "8.2"


# ---------------------------------------------------------------------------
# CLI — happy path with auto-detect
# ---------------------------------------------------------------------------


class TestCreateWithPhpAutoDetect:
    def test_auto_detect_produces_config_with_socket(self, mocker, tmp_path):
        """--php (no version) auto-detects and sets php_socket."""
        doc_root = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")

        mocker.patch(
            "vhost_helper.main.detect_default_version",
            return_value="8.2",
        )
        mocker.patch(
            "vhost_helper.main.resolve_socket_path",
            return_value="/run/php/php8.2-fpm.sock",
        )
        mocker.patch("vhost_helper.main.start_service", return_value=None)

        mock_provider = MagicMock()
        mocker.patch("vhost_helper.main._get_provider", return_value=mock_provider)

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc_root), "--php", "__auto__"],
        )

        assert result.exit_code == 0
        config: VHostConfig = mock_provider.create_vhost.call_args[0][0]
        assert config.php_socket == "/run/php/php8.2-fpm.sock"
        assert config.runtime == RuntimeMode.PHP


# ---------------------------------------------------------------------------
# CLI — missing version exits with code 1
# ---------------------------------------------------------------------------


class TestCreateWithMissingVersion:
    def test_absent_php_version_exits_1(self, mocker, tmp_path):
        """--php 7.4 with no socket or binary → exit code 1, no config written."""
        from vhost_helper.php_fpm import PhpFpmNotFoundError

        doc_root = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")

        mocker.patch(
            "vhost_helper.main.validate_version_present",
            side_effect=PhpFpmNotFoundError(
                "PHP-FPM version '7.4' not found. "
                "Expected socket: /run/php/php7.4-fpm.sock."
            ),
        )
        mock_provider = MagicMock()
        mocker.patch("vhost_helper.main._get_provider", return_value=mock_provider)

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc_root), "--php", "7.4"],
        )

        assert result.exit_code == 1

    def test_error_output_contains_version(self, mocker, tmp_path):
        from vhost_helper.php_fpm import PhpFpmNotFoundError

        doc_root = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")

        mocker.patch(
            "vhost_helper.main.validate_version_present",
            side_effect=PhpFpmNotFoundError(
                "PHP-FPM version '7.4' not found. "
                "Expected socket: /run/php/php7.4-fpm.sock."
            ),
        )
        mocker.patch("vhost_helper.main._get_provider", return_value=MagicMock())

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc_root), "--php", "7.4"],
        )

        assert "7.4" in result.output

    def test_error_output_contains_socket_path(self, mocker, tmp_path):
        from vhost_helper.php_fpm import PhpFpmNotFoundError

        doc_root = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")

        mocker.patch(
            "vhost_helper.main.validate_version_present",
            side_effect=PhpFpmNotFoundError(
                "PHP-FPM version '7.4' not found. "
                "Expected socket: /run/php/php7.4-fpm.sock."
            ),
        )
        mocker.patch("vhost_helper.main._get_provider", return_value=MagicMock())

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc_root), "--php", "7.4"],
        )

        assert "/run/php/php7.4-fpm.sock" in result.output

    def test_no_config_written_on_error(self, mocker, tmp_path):
        """No /etc/hosts entry or vhost config is written when version is absent."""
        from vhost_helper.php_fpm import PhpFpmNotFoundError

        doc_root = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mock_add_entry = mocker.patch("vhost_helper.main.add_entry")

        mocker.patch(
            "vhost_helper.main.validate_version_present",
            side_effect=PhpFpmNotFoundError("version 7.4 not found"),
        )
        mock_provider = MagicMock()
        mocker.patch("vhost_helper.main._get_provider", return_value=mock_provider)

        runner.invoke(
            app,
            ["create", "example.test", str(doc_root), "--php", "7.4"],
        )

        mock_add_entry.assert_not_called()
        mock_provider.create_vhost.assert_not_called()


# ---------------------------------------------------------------------------
# CLI — service start failure: warning shown, exit code 0
# ---------------------------------------------------------------------------


class TestPhpFpmServiceOrchestration:
    def test_service_failure_still_exits_0(self, mocker, tmp_path):
        """A failing systemctl call produces a warning but does not change exit code."""
        doc_root = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")

        mocker.patch(
            "vhost_helper.main.validate_version_present",
            return_value="/run/php/php8.2-fpm.sock",
        )
        mocker.patch(
            "vhost_helper.main.start_service",
            return_value="systemctl enable --now php8.2-fpm exited with code 1.",
        )
        mocker.patch("vhost_helper.main._get_provider", return_value=MagicMock())

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc_root), "--php", "8.2"],
        )

        assert result.exit_code == 0

    def test_service_failure_warning_contains_service_name(self, mocker, tmp_path):
        doc_root = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")

        mocker.patch(
            "vhost_helper.main.validate_version_present",
            return_value="/run/php/php8.2-fpm.sock",
        )
        mocker.patch(
            "vhost_helper.main.start_service",
            return_value="systemctl enable --now php8.2-fpm exited with code 1.",
        )
        mocker.patch("vhost_helper.main._get_provider", return_value=MagicMock())

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc_root), "--php", "8.2"],
        )

        assert "PHP-FPM Service Warning" in result.output or "php8.2-fpm" in result.output

    def test_service_success_no_warning(self, mocker, tmp_path):
        doc_root = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")

        mocker.patch(
            "vhost_helper.main.validate_version_present",
            return_value="/run/php/php8.2-fpm.sock",
        )
        mocker.patch("vhost_helper.main.start_service", return_value=None)
        mocker.patch("vhost_helper.main._get_provider", return_value=MagicMock())

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc_root), "--php", "8.2"],
        )

        assert result.exit_code == 0
        assert "PHP-FPM Service Warning" not in result.output


# ---------------------------------------------------------------------------
# Template rendering — Nginx
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template_name",
    ["default", "static", "nodejs-proxy", "php"],
)
class TestNginxTemplatePHP:
    def test_fastcgi_pass_present_when_socket_set(self, template_name):
        socket = "/run/php/php8.2-fpm.sock"
        kwargs = {}
        if template_name == "default":
            kwargs["runtime"] = "php"
        elif template_name == "php":
            kwargs["runtime"] = "php"
        rendered = _render_nginx(template_name, php_socket=socket, **kwargs)
        assert f"fastcgi_pass unix:{socket}" in rendered

    def test_fastcgi_pass_absent_when_socket_none(self, template_name):
        kwargs = {}
        if template_name in ("php",):
            # php.conf.j2 is a PHP-specific template and always renders PHP
            pytest.skip("php.conf.j2 is PHP-only; php_socket is always expected.")
        rendered = _render_nginx(template_name, php_socket=None, **kwargs)
        assert "fastcgi_pass" not in rendered
        assert r"\.php$" not in rendered


# ---------------------------------------------------------------------------
# Template rendering — Apache
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "template_name",
    ["default", "static", "nodejs-proxy"],
)
class TestApacheTemplatePHP:
    def test_filesMatch_present_when_socket_set(self, template_name):
        socket = "/run/php/php8.2-fpm.sock"
        kwargs = {}
        if template_name == "default":
            kwargs["runtime"] = "php"
        rendered = _render_apache(template_name, php_socket=socket, **kwargs)
        assert f'SetHandler "proxy:unix:{socket}|fcgi://localhost"' in rendered
        assert "<FilesMatch" in rendered

    def test_filesMatch_absent_when_socket_none(self, template_name):
        rendered = _render_apache(template_name, php_socket=None)
        assert "FilesMatch" not in rendered
        assert "SetHandler" not in rendered
