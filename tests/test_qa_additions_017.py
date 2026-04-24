import pytest
from typer.testing import CliRunner
from unittest.mock import patch

from vhost_helper.main import app
from vhost_helper.providers.nginx import NginxProvider
from vhost_helper.models import VHostConfig

runner = CliRunner()


@pytest.fixture
def vhost_config_obj(tmp_path):
    """Provides a basic VHostConfig for tests."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    return VHostConfig(domain="test.local", document_root=str(doc_root))


@pytest.fixture
def user_template_dir(tmp_path, monkeypatch):
    """Create a temporary user config directory and point the config to it."""
    user_dir = tmp_path / "user_config"
    user_dir.mkdir()

    # This is where our test will create its fake user templates
    user_templates = user_dir / "templates" / "nginx"
    user_templates.mkdir(parents=True)

    # Patch the config variables to point to our temporary directory
    monkeypatch.setattr(
        "vhost_helper.providers.nginx.USER_TEMPLATES_DIR", user_dir / "templates"
    )

    return user_templates


@pytest.fixture
def mock_provider_for_template_tests(mocker):
    """Fixture to provide a mocked NginxProvider for template-related tests."""
    mocker.patch("vhost_helper.providers.nginx.run_elevated_command")
    mocker.patch(
        "vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False
    )
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.main.add_entry")

    # Let the actual provider logic run, but mock away the things that touch the system
    provider = NginxProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")
    mocker.patch("vhost_helper.providers.nginx.NginxProvider", return_value=provider)
    return provider


def test_template_path_traversal_is_prevented(
    mock_provider_for_template_tests, tmp_path
):
    """
    Test that using path traversal in template names is handled gracefully
    and does not allow escaping the template directories.
    Jinja2's FileSystemLoader should prevent this by design.
    """
    # Setup dummy template directories
    user_templates = tmp_path / "user_templates/nginx"
    user_templates.mkdir(parents=True)
    app_templates = tmp_path / "app_templates/nginx"
    app_templates.mkdir(parents=True)
    (app_templates / "default.conf.j2").write_text("default")

    with patch(
        "vhost_helper.providers.nginx.USER_TEMPLATES_DIR", tmp_path / "user_templates"
    ), patch(
        "vhost_helper.providers.nginx.APP_TEMPLATES_DIR", tmp_path / "app_templates"
    ):

        provider = NginxProvider()

        with pytest.raises(FileNotFoundError) as excinfo:
            provider._get_template("../../../../../../../etc/passwd")

        assert "Template '../../../../../../../etc/passwd' not found" in str(
            excinfo.value
        )


def test_malformed_user_template_raises_error(tmp_path, mocker):
    """
    Test that a syntactically incorrect user template raises a Jinja2
    TemplateSyntaxError (propagated from _get_template at load time).
    A fresh NginxProvider is created *after* patching so that its jinja2
    environment actually points at the tmp directories.
    """
    from jinja2 import TemplateSyntaxError

    user_templates = tmp_path / "user_templates" / "nginx"
    user_templates.mkdir(parents=True)
    app_templates = tmp_path / "app_templates" / "nginx"
    app_templates.mkdir(parents=True)

    # Drop a syntactically invalid template in the user dir
    (user_templates / "malformed.conf.j2").write_text("server { {% if %} }")

    mocker.patch("vhost_helper.providers.nginx.run_elevated_command")
    mocker.patch(
        "vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False
    )

    with patch(
        "vhost_helper.providers.nginx.USER_TEMPLATES_DIR", tmp_path / "user_templates"
    ), patch(
        "vhost_helper.providers.nginx.APP_TEMPLATES_DIR", tmp_path / "app_templates"
    ):
        provider = NginxProvider()

    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="test.local", document_root=str(doc_root), template="malformed"
    )

    # Jinja2 parses templates at load time; TemplateSyntaxError propagates
    # out of _get_template before the try/except in create_vhost.
    with pytest.raises(TemplateSyntaxError):
        provider.create_vhost(config)


def test_config_dir_creation_permission_denied(tmp_path, monkeypatch):
    """
    Test that if creating the user config directory fails due to permissions,
    the application continues but template loading may fail later.
    """
    # Make the parent directory read-only
    read_only_home = tmp_path / "read_only_home"
    read_only_home.mkdir()

    # Set permissions to 555 (r-x r-x r-x)
    read_only_home.chmod(0o555)

    user_config_dir = read_only_home / ".config" / "vhost_helper"
    monkeypatch.setattr("vhost_helper.config.USER_CONFIG_DIR", user_config_dir)

    # We expect this to fail silently, not raising an exception,
    # to avoid crashing the app on startup.
    from vhost_helper.config import initialize_user_config

    try:
        initialize_user_config()
    except PermissionError:
        pytest.fail(
            "initialize_user_config should not raise PermissionError, but handle it gracefully."
        )

    # The directory should not have been created
    assert not user_config_dir.exists()


def test_config_path_is_a_file(tmp_path, monkeypatch, mocker):
    """
    Test that if the intended user config path is a file, the app doesn't crash.
    """
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    # Create a file where the config dir should be
    (home_dir / ".config").mkdir()
    config_file = home_dir / ".config" / "vhost_helper"
    config_file.write_text("I am a file, not a directory.")

    monkeypatch.setattr("vhost_helper.config.USER_CONFIG_DIR", config_file)

    from vhost_helper.config import initialize_user_config

    # The initialization should fail to create a directory but not crash.
    try:
        initialize_user_config()
    except Exception as e:
        pytest.fail(f"initialize_user_config crashed when path was a file: {e}")

    # Further check: trying to use a custom template should fail gracefully.
    # Mock all system-touching calls so the test doesn't block on sudo.
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch("vhost_helper.main.preflight_sudo_check")
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)

    doc_root = tmp_path / "www"
    doc_root.mkdir()
    result = runner.invoke(
        app, ["create", "file-test.local", str(doc_root), "--template", "custom"]
    )
    assert result.exit_code != 0
    assert "Template 'custom' not found" in result.stdout
