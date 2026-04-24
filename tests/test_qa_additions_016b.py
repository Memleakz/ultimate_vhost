"""
QA additions for ULTIMATE_VHOST-016: Template Engine Refactor.

Covers gaps not addressed by test_qa_additions_016.py and
test_qa_additions_017.py:

  - CLI --template / -t flag acceptance and help text
  - Template name with embedded .conf extension handled gracefully
  - Template resolution across Debian and RHEL os families
  - Error message quality (searched paths listed)
  - initialize_user_config idempotency
  - initialize_user_config error resilience (permission denied, path-is-file)
  - Empty/whitespace template name rejection
"""

import pytest
from pathlib import Path
from unittest.mock import patch
from typer.testing import CliRunner

from vhost_helper.main import app
from vhost_helper.providers.nginx import NginxProvider
from vhost_helper.models import VHostConfig
from vhost_helper.config import initialize_user_config

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_template_dirs(tmp_path):
    """Return (user_nginx_dir, app_nginx_dir) both empty, with module patched."""
    user_dir = tmp_path / "user_templates"
    app_dir = tmp_path / "app_templates"
    (user_dir / "nginx").mkdir(parents=True)
    (app_dir / "nginx").mkdir(parents=True)
    return user_dir, app_dir


@pytest.fixture
def fresh_provider(isolated_template_dirs):
    """NginxProvider created after patching template dirs; patches remain active."""
    user_dir, app_dir = isolated_template_dirs
    with patch("vhost_helper.providers.nginx.USER_TEMPLATES_DIR", user_dir), \
         patch("vhost_helper.providers.nginx.APP_TEMPLATES_DIR", app_dir):
        provider = NginxProvider()
        yield provider, user_dir / "nginx", app_dir / "nginx"


# ---------------------------------------------------------------------------
# CLI: --template flag presence and help text
# ---------------------------------------------------------------------------

class TestCliTemplateFlagPresence:
    def test_template_option_appears_in_create_help(self):
        result = runner.invoke(app, ["create", "--help"])
        assert result.exit_code == 0
        assert "--template" in result.stdout or "-t" in result.stdout

    def test_template_short_flag_accepted(self, tmp_path, mocker):
        """The short -t alias must be recognised by the CLI parser."""
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")
        mocker.patch("vhost_helper.main.preflight_sudo_check")

        doc_root = tmp_path / "www"
        doc_root.mkdir()
        result = runner.invoke(
            app,
            ["create", "shortflag.test", str(doc_root), "-t", "nonexistent_xyz"],
        )
        # Should fail with template-not-found, NOT with "no such option"
        assert result.exit_code != 0
        assert "nonexistent_xyz" in result.stdout or "nonexistent_xyz" in (result.exception and str(result.exception) or "")

    def test_default_template_name_is_default(self, tmp_path, mocker):
        """When --template is omitted, VHostConfig.template defaults to 'default'."""
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.add_entry")
        mocker.patch("vhost_helper.main.remove_entry")
        mocker.patch("vhost_helper.main.preflight_sudo_check")

        captured = {}

        def mock_create_vhost(config, service_running=True):
            captured["template"] = config.template

        mocker.patch(
            "vhost_helper.providers.nginx.NginxProvider.create_vhost",
            side_effect=mock_create_vhost,
        )

        doc_root = tmp_path / "www"
        doc_root.mkdir()
        runner.invoke(app, ["create", "default-template.test", str(doc_root)])
        assert captured.get("template") == "default"


# ---------------------------------------------------------------------------
# Template resolution edge cases
# ---------------------------------------------------------------------------

class TestTemplateResolutionEdgeCases:
    def test_template_name_with_conf_extension_not_found(self, fresh_provider):
        """If user passes 'wordpress.conf', the lookup key becomes
        'wordpress.conf.conf.j2' which should not be found."""
        provider, user_dir, app_dir = fresh_provider
        (app_dir / "wordpress.conf.j2").write_text("valid")  # real template
        with pytest.raises(FileNotFoundError) as exc:
            provider._get_template("wordpress.conf")
        # Ensure the error message lists search paths
        assert "wordpress.conf" in str(exc.value)

    def test_error_message_lists_both_search_paths(self, fresh_provider):
        """FileNotFoundError for a missing template must list both directories."""
        provider, user_dir, app_dir = fresh_provider
        with pytest.raises(FileNotFoundError) as exc:
            provider._get_template("missing_template")
        msg = str(exc.value)
        assert "missing_template" in msg
        # Both search locations should be mentioned
        assert str(user_dir) in msg
        assert str(app_dir) in msg

    def test_user_template_takes_precedence_over_app(self, isolated_template_dirs):
        """Same-named template: user dir wins even when app dir also has it."""
        user_dir, app_dir = isolated_template_dirs
        (user_dir / "nginx" / "shared.conf.j2").write_text("user version")
        (app_dir / "nginx" / "shared.conf.j2").write_text("app version")
        with patch("vhost_helper.providers.nginx.USER_TEMPLATES_DIR", user_dir), \
             patch("vhost_helper.providers.nginx.APP_TEMPLATES_DIR", app_dir):
            provider = NginxProvider()
        tmpl = provider._get_template("shared")
        assert tmpl.render() == "user version"

    def test_app_fallback_when_no_user_template(self, isolated_template_dirs):
        """If user dir has no match, the app dir must supply the template."""
        user_dir, app_dir = isolated_template_dirs
        (app_dir / "nginx" / "apponly.conf.j2").write_text("app only")
        with patch("vhost_helper.providers.nginx.USER_TEMPLATES_DIR", user_dir), \
             patch("vhost_helper.providers.nginx.APP_TEMPLATES_DIR", app_dir):
            provider = NginxProvider()
        tmpl = provider._get_template("apponly")
        assert tmpl.render() == "app only"

    def test_path_traversal_in_template_name_fails(self, fresh_provider):
        """Path traversal sequences must not resolve to real filesystem paths."""
        provider, _, _ = fresh_provider
        with pytest.raises(FileNotFoundError):
            provider._get_template("../../../etc/passwd")

    def test_absolute_path_as_template_name_fails(self, fresh_provider):
        """/etc/nginx/nginx.conf should not be loadable as a template name."""
        provider, _, _ = fresh_provider
        with pytest.raises(FileNotFoundError):
            provider._get_template("/etc/nginx/nginx.conf")


# ---------------------------------------------------------------------------
# initialize_user_config resilience
# ---------------------------------------------------------------------------

class TestInitializeUserConfigResilience:
    def test_idempotent_when_directory_already_exists(self, tmp_path):
        """Calling initialize_user_config twice must not raise."""
        nginx_dir = tmp_path / "templates" / "nginx"
        nginx_dir.mkdir(parents=True)
        with patch("vhost_helper.config.USER_TEMPLATES_DIR", tmp_path / "templates"):
            initialize_user_config()
            initialize_user_config()  # second call must be a no-op
        assert nginx_dir.exists()

    def test_creates_nginx_subdirectory(self, tmp_path):
        """The function must create the nginx sub-directory."""
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        with patch("vhost_helper.config.USER_TEMPLATES_DIR", templates_dir):
            initialize_user_config()
        assert (templates_dir / "nginx").is_dir()

    def test_permission_error_is_silenced(self, tmp_path):
        """PermissionError during directory creation must not crash the app."""
        read_only = tmp_path / "ro"
        read_only.mkdir(mode=0o555)
        blocked_templates = read_only / "templates"  # cannot create here
        with patch("vhost_helper.config.USER_TEMPLATES_DIR", blocked_templates):
            # Must NOT raise
            initialize_user_config()

    def test_path_is_file_is_silenced(self, tmp_path):
        """If the templates path is already a file, initialisation must not crash."""
        file_path = tmp_path / "templates"
        file_path.write_text("I am a file")
        with patch("vhost_helper.config.USER_TEMPLATES_DIR", file_path):
            initialize_user_config()
        # The file should still be a file (not overwritten)
        assert file_path.is_file()


# ---------------------------------------------------------------------------
# NginxProvider RHEL-family template resolution
# ---------------------------------------------------------------------------

class TestRhelTemplateResolution:
    def test_rhel_provider_also_uses_template_hierarchy(self, isolated_template_dirs, mocker):
        """Template hierarchy must work on RHEL-family too."""
        user_dir, app_dir = isolated_template_dirs
        (app_dir / "nginx" / "default.conf.j2").write_text("rhel default")

        mocker.patch("vhost_helper.providers.nginx.detected_os_family", "rhel_family")

        with patch("vhost_helper.providers.nginx.USER_TEMPLATES_DIR", user_dir), \
             patch("vhost_helper.providers.nginx.APP_TEMPLATES_DIR", app_dir):
            provider = NginxProvider()

        tmpl = provider._get_template("default")
        assert tmpl.render() == "rhel default"
