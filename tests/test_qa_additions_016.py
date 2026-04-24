import pytest
from pathlib import Path
from unittest.mock import patch
from vhost_helper.providers.nginx import NginxProvider
from vhost_helper.models import VHostConfig
from vhost_helper.config import initialize_user_config

# This is a sample of what the default template might contain
DEFAULT_TEMPLATE_CONTENT = (
    "server { listen 80; server_name {{ domain }}; root {{ document_root }}; }"
)
# This is a sample for a custom user template
CUSTOM_DEFAULT_TEMPLATE_CONTENT = "server { listen 80; server_name {{ domain }}; root {{ document_root }}; # Custom override }"
# This is a sample for a named template (e.g., wordpress)
WORDPRESS_TEMPLATE_CONTENT = "server { listen 80; server_name {{ domain }}; root {{ document_root }}; # Wordpress template }"


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
def app_template_dir(tmp_path, monkeypatch):
    """Create a temporary app template directory."""
    app_dir = tmp_path / "app_templates"
    app_dir.mkdir()

    app_nginx_templates = app_dir / "nginx"
    app_nginx_templates.mkdir(parents=True)

    # Create a default template file
    (app_nginx_templates / "default.conf.j2").write_text(DEFAULT_TEMPLATE_CONTENT)

    monkeypatch.setattr("vhost_helper.providers.nginx.APP_TEMPLATES_DIR", app_dir)
    return app_nginx_templates


@pytest.fixture
def vhost_config_obj(tmp_path):
    """Provides a basic VHostConfig for tests."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    return VHostConfig(domain="test.local", document_root=str(doc_root))


@pytest.fixture
def mock_provider(user_template_dir, app_template_dir, mocker):
    """Fixture to provide a mocked NginxProvider."""
    # Mock external commands and system state
    mocker.patch("vhost_helper.providers.nginx.run_elevated_command")
    mocker.patch(
        "vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False
    )
    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "debian_family")
    mocker.patch(
        "vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE",
        Path("/fake/sites-available"),
    )
    mocker.patch(
        "vhost_helper.providers.nginx.NGINX_SITES_ENABLED", Path("/fake/sites-enabled")
    )

    provider = NginxProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")

    return provider


def test_uses_default_app_template(mock_provider, vhost_config_obj):
    """Test that the default application template is used when no custom template exists."""
    template = mock_provider._get_template("default")
    rendered = template.render(
        domain=vhost_config_obj.domain, document_root=vhost_config_obj.document_root
    )
    expected = DEFAULT_TEMPLATE_CONTENT.replace(
        "{{ domain }}", vhost_config_obj.domain
    ).replace("{{ document_root }}", str(vhost_config_obj.document_root))
    assert rendered == expected


def test_uses_custom_user_template_override(
    mock_provider, vhost_config_obj, user_template_dir
):
    """Test that a user-provided template overrides the default one."""
    (user_template_dir / "default.conf.j2").write_text(CUSTOM_DEFAULT_TEMPLATE_CONTENT)

    template = mock_provider._get_template("default")
    rendered = template.render(
        domain=vhost_config_obj.domain, document_root=vhost_config_obj.document_root
    )
    expected = CUSTOM_DEFAULT_TEMPLATE_CONTENT.replace(
        "{{ domain }}", vhost_config_obj.domain
    ).replace("{{ document_root }}", str(vhost_config_obj.document_root))
    assert rendered == expected


def test_uses_named_template_from_app_dir(
    mock_provider, vhost_config_obj, app_template_dir
):
    """Test that a named template from the app directory is resolved."""
    (app_template_dir / "wordpress.conf.j2").write_text(WORDPRESS_TEMPLATE_CONTENT)

    template = mock_provider._get_template("wordpress")
    rendered = template.render(
        domain=vhost_config_obj.domain, document_root=vhost_config_obj.document_root
    )
    expected = WORDPRESS_TEMPLATE_CONTENT.replace(
        "{{ domain }}", vhost_config_obj.domain
    ).replace("{{ document_root }}", str(vhost_config_obj.document_root))
    assert rendered == expected


def test_uses_named_template_from_user_dir(
    mock_provider, vhost_config_obj, user_template_dir
):
    """Test that a named template from the user directory is resolved."""
    (user_template_dir / "wordpress.conf.j2").write_text(WORDPRESS_TEMPLATE_CONTENT)

    template = mock_provider._get_template("wordpress")
    rendered = template.render(
        domain=vhost_config_obj.domain, document_root=vhost_config_obj.document_root
    )
    expected = WORDPRESS_TEMPLATE_CONTENT.replace(
        "{{ domain }}", vhost_config_obj.domain
    ).replace("{{ document_root }}", str(vhost_config_obj.document_root))
    assert rendered == expected


def test_user_named_template_overrides_app_template(
    mock_provider, vhost_config_obj, user_template_dir, app_template_dir
):
    """Test that a user-provided named template overrides the app's named template."""
    app_wordpress_content = "App Wordpress Template"
    user_wordpress_content = "User Wordpress Template"
    (app_template_dir / "wordpress.conf.j2").write_text(app_wordpress_content)
    (user_template_dir / "wordpress.conf.j2").write_text(user_wordpress_content)

    template = mock_provider._get_template("wordpress")
    rendered = template.render(
        domain=vhost_config_obj.domain, document_root=vhost_config_obj.document_root
    )
    assert rendered == user_wordpress_content


def test_raises_error_for_nonexistent_template(mock_provider):
    """Test that a FileNotFoundError is raised for a template that does not exist."""
    with pytest.raises(FileNotFoundError) as excinfo:
        mock_provider._get_template("nonexistent")

    assert "Template 'nonexistent' not found" in str(excinfo.value)
    assert "nonexistent.conf.j2" in str(excinfo.value)


def test_initialize_user_config_creates_directory(tmp_path):
    """Test that the initialization function creates the user config directory."""
    user_config_dir = tmp_path / "user_home" / ".config" / "vhost_helper"

    with patch("pathlib.Path.home", return_value=tmp_path / "user_home"):
        # We need to re-import or reload the config module for the patch to take effect
        # on the USER_TEMPLATES_DIR global. For simplicity in a test, we can patch it directly.
        with patch(
            "vhost_helper.config.USER_TEMPLATES_DIR", user_config_dir / "templates"
        ):
            initialize_user_config()

    expected_dir = user_config_dir / "templates" / "nginx"
    assert expected_dir.exists()
    assert expected_dir.is_dir()
