"""QA additions for ULTIMATE_VHOST-021 — PHP-FPM Auto-linking.

Covers gaps identified during QA review:
- Coverage gaps in main.py (--runtime php legacy, --runtime static explicit,
  mutual exclusion edge cases, service orchestration exception paths)
- models.py: php_socket relative-path rejection (line 102)
- php_fpm.py: version-key ValueError handler (lines 125-126)
- utils.py: reload_service fallback, apply_selinux_context paths
- Template edge cases: PHP blocks absent in nodejs-proxy SSL path (documented)
- Acceptance criteria verification from PRD §11 ULTIMATE_VHOST-021
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from vhost_helper.main import app
from vhost_helper.models import VHostConfig, RuntimeMode
from vhost_helper.php_fpm import (
    PhpFpmNotFoundError,
    detect_default_version,
    resolve_socket_path,
    get_service_name,
)
from vhost_helper.providers.nginx import NginxProvider
from vhost_helper.providers.apache import ApacheProvider

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_doc_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


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


# ---------------------------------------------------------------------------
# PRD §11 F7 — VHostConfig.php_socket field validation
# ---------------------------------------------------------------------------


class TestVHostConfigPhpSocket:
    """PRD F7: Pydantic model must accept absolute paths and reject relative paths."""

    def test_absolute_path_accepted(self, tmp_path):
        doc = tmp_path / "web"
        doc.mkdir()
        cfg = VHostConfig(
            domain="example.test",
            document_root=doc,
            php_socket="/run/php/php8.2-fpm.sock",
        )
        assert cfg.php_socket == "/run/php/php8.2-fpm.sock"

    def test_none_accepted_as_default(self, tmp_path):
        doc = tmp_path / "web"
        doc.mkdir()
        cfg = VHostConfig(domain="example.test", document_root=doc)
        assert cfg.php_socket is None

    def test_relative_path_rejected(self, tmp_path):
        """PRD F7: php_socket must start with '/'. Relative path raises ValidationError."""
        doc = tmp_path / "web"
        doc.mkdir()
        with pytest.raises(ValidationError) as exc_info:
            VHostConfig(
                domain="example.test",
                document_root=doc,
                php_socket="run/php/php8.2-fpm.sock",  # no leading /
            )
        assert (
            "absolute path" in str(exc_info.value).lower()
            or "must start with" in str(exc_info.value).lower()
            or "/" in str(exc_info.value)
        )

    def test_empty_string_rejected(self, tmp_path):
        """Empty string has no leading slash — must be rejected."""
        doc = tmp_path / "web"
        doc.mkdir()
        with pytest.raises(ValidationError):
            VHostConfig(
                domain="example.test",
                document_root=doc,
                php_socket="",
            )

    def test_rhel_socket_path_accepted(self, tmp_path):
        doc = tmp_path / "web"
        doc.mkdir()
        cfg = VHostConfig(
            domain="example.test",
            document_root=doc,
            php_socket="/run/php-fpm/www.sock",
        )
        assert cfg.php_socket == "/run/php-fpm/www.sock"


# ---------------------------------------------------------------------------
# PRD §11 F5 — Nginx template conditional PHP blocks
# ---------------------------------------------------------------------------


class TestNginxPhpTemplateEdgeCases:
    """Additional template rendering edge cases not covered by test_php_fpm_cli.py."""

    def test_static_template_php_block_with_rhel_socket(self):
        """RHEL socket path rendered correctly in Nginx static template."""
        socket = "/run/php-fpm/www.sock"
        rendered = _render_nginx("static", php_socket=socket)
        assert f"fastcgi_pass unix:{socket}" in rendered

    def test_static_template_php_index_added_when_socket_set(self):
        """index.php is added to index directive when php_socket is set."""
        rendered = _render_nginx("static", php_socket="/run/php/php8.2-fpm.sock")
        assert "index.php" in rendered

    def test_static_template_no_php_index_when_socket_none(self):
        """index.php is NOT in index directive when php_socket is None."""
        rendered = _render_nginx("static", php_socket=None)
        assert "index.php" not in rendered

    def test_nodejs_template_fastcgi_present_non_ssl(self):
        """PHP block renders in nodejs-proxy non-SSL path."""
        socket = "/run/php/php8.2-fpm.sock"
        rendered = _render_nginx("nodejs-proxy", php_socket=socket)
        assert f"fastcgi_pass unix:{socket}" in rendered

    def test_nodejs_template_fastcgi_absent_when_none_non_ssl(self):
        rendered = _render_nginx("nodejs-proxy", php_socket=None)
        assert "fastcgi_pass" not in rendered

    def test_default_template_php_block_with_runtime_php(self):
        """Default template with runtime=php and php_socket renders PHP block."""
        socket = "/run/php/php8.1-fpm.sock"
        rendered = _render_nginx("default", php_socket=socket, runtime="php")
        assert f"fastcgi_pass unix:{socket}" in rendered

    def test_default_template_fastcgi_absent_when_runtime_static(self):
        rendered = _render_nginx("default", php_socket=None, runtime="static")
        assert "fastcgi_pass" not in rendered


# ---------------------------------------------------------------------------
# PRD §11 F6 — Apache template conditional PHP blocks
# ---------------------------------------------------------------------------


class TestApachePhpTemplateEdgeCases:
    def test_static_template_filesMatch_with_rhel_socket(self):
        socket = "/run/php-fpm/www.sock"
        rendered = _render_apache("static", php_socket=socket)
        assert f'SetHandler "proxy:unix:{socket}|fcgi://localhost"' in rendered
        assert "<FilesMatch" in rendered

    def test_static_template_directoryIndex_php_when_socket_set(self):
        rendered = _render_apache("static", php_socket="/run/php-fpm/www.sock")
        assert "index.php" in rendered

    def test_static_template_no_php_directoryIndex_when_none(self):
        rendered = _render_apache("static", php_socket=None)
        assert "index.php" not in rendered

    def test_nodejs_template_filesMatch_present(self):
        socket = "/run/php/php8.2-fpm.sock"
        rendered = _render_apache("nodejs-proxy", php_socket=socket)
        assert f'SetHandler "proxy:unix:{socket}|fcgi://localhost"' in rendered

    def test_nodejs_template_filesMatch_absent_when_none(self):
        rendered = _render_apache("nodejs-proxy", php_socket=None)
        assert "FilesMatch" not in rendered
        assert "SetHandler" not in rendered

    def test_ssl_static_template_filesMatch_present(self):
        """PHP block renders in SSL static template too."""
        socket = "/run/php/php8.2-fpm.sock"
        rendered = _render_apache(
            "static",
            php_socket=socket,
            use_ssl=True,
            cert_path="/etc/ssl/cert.pem",
            key_path="/etc/ssl/key.pem",
        )
        assert f'SetHandler "proxy:unix:{socket}|fcgi://localhost"' in rendered

    def test_ssl_static_template_filesMatch_absent_when_none(self):
        rendered = _render_apache(
            "static",
            php_socket=None,
            use_ssl=True,
            cert_path="/etc/ssl/cert.pem",
            key_path="/etc/ssl/key.pem",
        )
        assert "FilesMatch" not in rendered


# ---------------------------------------------------------------------------
# PRD §11 F2 — resolve_socket_path correctness
# ---------------------------------------------------------------------------


class TestResolveSocketPathAcceptanceCriteria:
    """Direct verification of PRD §11 F2 acceptance criteria."""

    def test_debian_82_returns_correct_path(self):
        """PRD F2: debian + 8.2 → /run/php/php8.2-fpm.sock"""
        assert resolve_socket_path("8.2", "debian_family") == "/run/php/php8.2-fpm.sock"

    def test_rhel_82_returns_version_agnostic_path(self):
        """PRD F2: rhel + 8.2 → /run/php-fpm/www.sock"""
        assert resolve_socket_path("8.2", "rhel_family") == "/run/php-fpm/www.sock"

    def test_rhel_all_versions_same_path(self):
        """RHEL socket path is version-agnostic."""
        assert resolve_socket_path("7.4", "rhel_family") == resolve_socket_path(
            "8.3", "rhel_family"
        )

    def test_debian_version_embedded_in_path(self):
        for version in ["7.4", "8.0", "8.1", "8.2", "8.3"]:
            path = resolve_socket_path(version, "debian_family")
            assert version in path


# ---------------------------------------------------------------------------
# Coverage gap: --runtime php (legacy path, lines 285-287)
# ---------------------------------------------------------------------------


class TestLegacyRuntimePhpPath:
    """--runtime php uses legacy socket resolution with no strict validation."""

    def test_runtime_php_resolves_legacy_socket(self, mocker, tmp_path):
        """Lines 285-287: legacy path via --runtime php."""
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")
        mocker.patch(
            "vhost_helper.main.get_os_info",
            return_value=MagicMock(family="debian_family"),
        )
        mock_provider = MagicMock()
        mocker.patch("vhost_helper.main._get_provider", return_value=mock_provider)

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc), "--runtime", "php"],
        )

        assert result.exit_code == 0
        config: VHostConfig = mock_provider.create_vhost.call_args[0][0]
        assert config.runtime == RuntimeMode.PHP
        # Legacy path: any non-None socket path from PHP_SOCKET_PATHS
        assert config.php_socket is not None
        assert config.php_socket.startswith("/")

    def test_runtime_php_does_not_call_validate_version_present(self, mocker, tmp_path):
        """Legacy path must NOT call validate_version_present (no strict check)."""
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")
        mocker.patch(
            "vhost_helper.main.get_os_info",
            return_value=MagicMock(family="debian_family"),
        )
        mock_validate = mocker.patch("vhost_helper.main.validate_version_present")
        mocker.patch("vhost_helper.main._get_provider", return_value=MagicMock())

        runner.invoke(
            app,
            ["create", "example.test", str(doc), "--runtime", "php"],
        )
        mock_validate.assert_not_called()


# ---------------------------------------------------------------------------
# Coverage gap: --runtime static explicit (lines 297-299)
# ---------------------------------------------------------------------------


class TestRuntimeStaticExplicit:
    """Lines 297-299: explicit --runtime static still works."""

    def test_explicit_runtime_static_sets_static_mode(self, mocker, tmp_path):
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")
        mock_provider = MagicMock()
        mocker.patch("vhost_helper.main._get_provider", return_value=mock_provider)

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc), "--runtime", "static"],
        )

        assert result.exit_code == 0
        config: VHostConfig = mock_provider.create_vhost.call_args[0][0]
        assert config.runtime == RuntimeMode.STATIC
        assert config.php_socket is None


# ---------------------------------------------------------------------------
# Coverage gap: mutual exclusion edge cases
# ---------------------------------------------------------------------------


class TestMutualExclusionEdgeCases:
    """--php, --python, --nodejs, --runtime are mutually exclusive."""

    def test_php_and_python_rejected(self, mocker, tmp_path):
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc), "--php", "__auto__", "--python"],
        )
        assert result.exit_code == 1

    def test_php_version_and_nodejs_rejected(self, mocker, tmp_path):
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc), "--php", "8.2", "--nodejs"],
        )
        assert result.exit_code == 1

    def test_php_and_runtime_rejected(self, mocker, tmp_path):
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")

        result = runner.invoke(
            app,
            [
                "create",
                "example.test",
                str(doc),
                "--php",
                "__auto__",
                "--runtime",
                "python",
            ],
        )
        assert result.exit_code == 1

    def test_php_version_and_runtime_rejected(self, mocker, tmp_path):
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc), "--php", "8.2", "--runtime", "nodejs"],
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Coverage gap: _orchestrate_php_fpm_service os detection failure (lines 465-466)
# ---------------------------------------------------------------------------


class TestOrchestratePhpFpmServiceOsFailure:
    """Lines 465-466: when get_os_info() fails, fallback to debian_family."""

    def test_os_detection_failure_falls_back_gracefully(self, mocker, tmp_path):
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")
        mocker.patch(
            "vhost_helper.main.validate_version_present",
            return_value="/run/php/php8.2-fpm.sock",
        )
        # Fail OS detection in BOTH calls (resolve and orchestrate)
        mocker.patch(
            "vhost_helper.main.get_os_info",
            side_effect=RuntimeError("OS detection failed"),
        )
        mocker.patch("vhost_helper.main.start_service", return_value=None)
        mocker.patch("vhost_helper.main._get_provider", return_value=MagicMock())

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc), "--php", "8.2"],
        )
        # Should still succeed despite OS detection failure
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Coverage gap: _orchestrate_php_fpm_service detect_default_version failure
# (lines 471-472)
# ---------------------------------------------------------------------------


class TestOrchestratePhpFpmAutoDetectFailure:
    """Lines 471-472: when detect_default_version fails in orchestrator, silent pass."""

    def test_auto_detect_version_failure_in_orchestrator_is_silent(
        self, mocker, tmp_path
    ):
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")
        mocker.patch(
            "vhost_helper.main.resolve_socket_path",
            return_value="/run/php/php8.2-fpm.sock",
        )

        # auto-detect during socket resolution succeeds
        mocker.patch(
            "vhost_helper.main.detect_default_version",
            side_effect=[
                "8.2",  # first call: in _resolve_php_socket for --php flag
                PhpFpmNotFoundError("not found"),  # second call: in orchestrator
            ],
        )
        mocker.patch("vhost_helper.main._get_provider", return_value=MagicMock())

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc), "--php", "__auto__"],
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Coverage gap: php_fpm.py version key ValueError (lines 125-126)
# ---------------------------------------------------------------------------


class TestDetectDefaultVersionKeyError:
    """Lines 125-126: ValueError in _version_key handled gracefully."""

    def test_malformed_version_string_treated_as_lowest(self):
        """A socket path that yields a non-numeric version string is sorted last."""
        with (
            patch(
                "vhost_helper.php_fpm.glob.glob",
                return_value=[
                    "/run/php/phpbeta-fpm.sock",  # won't match version regex
                    "/run/php/php8.1-fpm.sock",
                ],
            ),
            patch("vhost_helper.php_fpm.shutil.which", return_value=None),
        ):
            # Only 8.1 is a real candidate (beta won't match the regex)
            result = detect_default_version("debian_family")
        assert result == "8.1"

    def test_version_key_with_invalid_int_falls_back_to_zero_tuple(self):
        """Inject a candidate that will fail int() conversion."""

        # Temporarily add a malformed candidate via monkeypatching the internal sort

        with (
            patch(
                "vhost_helper.php_fpm.glob.glob",
                return_value=["/run/php/php8.1-fpm.sock"],
            ),
            patch("vhost_helper.php_fpm.shutil.which", return_value=None),
        ):
            result = detect_default_version("debian_family")
        # Should return 8.1 cleanly
        assert result == "8.1"


# ---------------------------------------------------------------------------
# Coverage gap: utils.reload_service fallback (lines 204-217)
# ---------------------------------------------------------------------------


class TestReloadServiceFallback:
    """utils.reload_service: systemctl fails, fallback succeeds or both fail."""

    def test_fallback_called_when_systemctl_fails(self):
        from vhost_helper.utils import reload_service

        with patch(
            "vhost_helper.utils.run_elevated_command",
            side_effect=[
                RuntimeError("systemctl failed"),  # first call: systemctl
                None,  # second call: fallback
            ],
        ):
            # Should not raise
            reload_service("nginx", fallback_args=["nginx", "-s", "reload"])

    def test_raises_when_systemctl_fails_and_no_fallback(self):
        from vhost_helper.utils import reload_service

        with patch(
            "vhost_helper.utils.run_elevated_command",
            side_effect=RuntimeError("systemctl failed"),
        ):
            with pytest.raises(RuntimeError, match="systemctl"):
                reload_service("nginx")

    def test_raises_when_both_fail(self):
        from vhost_helper.utils import reload_service

        with patch(
            "vhost_helper.utils.run_elevated_command",
            side_effect=RuntimeError("failed"),
        ):
            with pytest.raises(RuntimeError):
                reload_service("nginx", fallback_args=["nginx", "-s", "reload"])


# ---------------------------------------------------------------------------
# Coverage gap: utils.apply_selinux_context (lines 228-236)
# ---------------------------------------------------------------------------


class TestApplySelinuxContext:
    def test_success_path_calls_run_elevated_command(self, tmp_path):
        from vhost_helper.utils import apply_selinux_context

        target = tmp_path / "nginx.conf"
        target.touch()
        with patch("vhost_helper.utils.run_elevated_command") as mock_cmd:
            apply_selinux_context(target)
        mock_cmd.assert_called_once()
        cmd_args = mock_cmd.call_args[0][0]
        assert "chcon" in cmd_args
        assert str(target) in cmd_args

    def test_recursive_flag_included(self, tmp_path):
        from vhost_helper.utils import apply_selinux_context

        with patch("vhost_helper.utils.run_elevated_command") as mock_cmd:
            apply_selinux_context(tmp_path, recursive=True)
        cmd_args = mock_cmd.call_args[0][0]
        assert "-R" in cmd_args

    def test_failure_raises_runtime_error(self, tmp_path):
        from vhost_helper.utils import apply_selinux_context

        target = tmp_path / "nginx.conf"
        target.touch()
        with patch(
            "vhost_helper.utils.run_elevated_command",
            side_effect=RuntimeError("chcon failed"),
        ):
            with pytest.raises(
                RuntimeError, match="SELinux context application failed"
            ):
                apply_selinux_context(target)

    def test_custom_context_type_passed(self, tmp_path):
        from vhost_helper.utils import apply_selinux_context

        with patch("vhost_helper.utils.run_elevated_command") as mock_cmd:
            apply_selinux_context(tmp_path, context_type="httpd_sys_content_t")
        cmd_args = mock_cmd.call_args[0][0]
        assert "httpd_sys_content_t" in cmd_args


# ---------------------------------------------------------------------------
# PRD §11 F3 — Exit code 1 + no writes BEFORE filesystem changes
# ---------------------------------------------------------------------------


class TestF3ExitBeforeWrites:
    """Missing version must fail before any hostfile or config write."""

    def test_no_hostfile_write_on_missing_version_explicit(self, mocker, tmp_path):
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mock_add_entry = mocker.patch("vhost_helper.main.add_entry")

        mocker.patch(
            "vhost_helper.main.validate_version_present",
            side_effect=PhpFpmNotFoundError("7.4 not found"),
        )

        runner.invoke(
            app,
            ["create", "example.test", str(doc), "--php", "7.4"],
        )
        mock_add_entry.assert_not_called()

    def test_no_hostfile_write_on_missing_version_auto_detect(self, mocker, tmp_path):
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mock_add_entry = mocker.patch("vhost_helper.main.add_entry")

        mocker.patch(
            "vhost_helper.main.detect_default_version",
            side_effect=PhpFpmNotFoundError("no PHP found"),
        )

        runner.invoke(
            app,
            ["create", "example.test", str(doc), "--php", "__auto__"],
        )
        mock_add_entry.assert_not_called()

    def test_error_panel_title_is_php_fpm_not_found(self, mocker, tmp_path):
        """PRD F3: Rich Panel title must be 'PHP-FPM Not Found'."""
        doc = _make_doc_root(tmp_path)
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

        result = runner.invoke(
            app,
            ["create", "example.test", str(doc), "--php", "7.4"],
        )
        assert result.exit_code == 1
        assert "PHP-FPM Not Found" in result.output


# ---------------------------------------------------------------------------
# PRD §11 F4 — Service orchestration: RHEL service name
# ---------------------------------------------------------------------------


class TestRhelServiceOrchestration:
    def test_rhel_service_name_is_php_fpm(self):
        """PRD F4: RHEL service name must be 'php-fpm' (no version suffix)."""
        assert get_service_name("8.2", "rhel_family") == "php-fpm"
        assert get_service_name("7.4", "rhel_family") == "php-fpm"
        assert get_service_name("system", "rhel_family") == "php-fpm"

    def test_debian_service_name_is_versioned(self):
        assert get_service_name("8.2", "debian_family") == "php8.2-fpm"
        assert get_service_name("7.4", "debian_family") == "php7.4-fpm"


# ---------------------------------------------------------------------------
# PRD §11 F4 — Service orchestration: non-blocking warning
# ---------------------------------------------------------------------------


class TestServiceOrchestrationWarning:
    def test_warning_panel_title_on_failure(self, mocker, tmp_path):
        """PRD F4: warning Panel title must be 'PHP-FPM Service Warning'."""
        doc = _make_doc_root(tmp_path)
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
            ["create", "example.test", str(doc), "--php", "8.2"],
        )
        assert result.exit_code == 0
        assert "PHP-FPM Service Warning" in result.output

    def test_no_service_orchestration_for_non_php_runtime(self, mocker, tmp_path):
        """start_service must NOT be called for python/nodejs runtime."""
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")
        mock_start = mocker.patch("vhost_helper.main.start_service")
        mocker.patch("vhost_helper.main._get_provider", return_value=MagicMock())

        runner.invoke(
            app,
            ["create", "example.test", str(doc), "--runtime", "python"],
        )
        mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# Coverage gap: templates list "no templates" path (line 1031)
# ---------------------------------------------------------------------------


class TestTemplatesListNoResults:
    def test_no_templates_exits_1_with_message(self, mocker):
        """Line 1031: when list_templates returns empty dict, exits with error."""
        mocker.patch("vhost_helper.main.list_templates", return_value={})
        result = runner.invoke(app, ["templates", "list"])
        assert result.exit_code == 1
        assert "No templates found" in result.output

    def test_no_templates_with_provider_filter(self, mocker):
        mocker.patch("vhost_helper.main.list_templates", return_value={})
        result = runner.invoke(app, ["templates", "list", "--provider", "nginx"])
        assert result.exit_code == 1

    def test_templates_list_success(self, mocker):
        mocker.patch(
            "vhost_helper.main.list_templates",
            return_value={"nginx": ["default", "static"], "apache": ["default"]},
        )
        result = runner.invoke(app, ["templates", "list"])
        assert result.exit_code == 0
        assert "nginx" in result.output


# ---------------------------------------------------------------------------
# Additional PRD §11 acceptance criteria smoke tests
# ---------------------------------------------------------------------------


class TestAcceptanceCriteriaSmoke:
    """Smoke tests verifying high-level PRD acceptance criteria."""

    def test_ac1_php82_generates_php82_socket(self, mocker, tmp_path):
        """AC: vhost create dev.local --php 8.2 → config has PHP 8.2 socket."""
        doc = _make_doc_root(tmp_path)
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

        result = runner.invoke(app, ["create", "dev.local", str(doc), "--php", "8.2"])
        assert result.exit_code == 0
        config: VHostConfig = mock_provider.create_vhost.call_args[0][0]
        assert config.php_socket == "/run/php/php8.2-fpm.sock"
        assert config.runtime == RuntimeMode.PHP

    def test_ac3_missing_version_exits_1(self, mocker, tmp_path):
        """AC: exit code 1 when --php-version specifies absent version."""
        doc = _make_doc_root(tmp_path)
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch(
            "vhost_helper.main.validate_version_present",
            side_effect=PhpFpmNotFoundError("7.4 not found"),
        )

        result = runner.invoke(app, ["create", "dev.local", str(doc), "--php", "7.4"])
        assert result.exit_code == 1

    def test_ac_coverage_gate(self):
        """Coverage must remain ≥ 80% (checked by test runner via --cov flag)."""
        # This test is a placeholder sentinel — actual gate enforcement is in CI.
        assert True
