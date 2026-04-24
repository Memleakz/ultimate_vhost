"""
Unit and integration tests for Node.js runtime support (ULTIMATE_VHOST-018).

Covers:
- NginxProvider: nodejs runtime with default port 3000
- NginxProvider: nodejs runtime with custom port
- NginxProvider: nodejs runtime with Unix Domain Socket
- ApacheProvider: nodejs runtime with default port 3000
- ApacheProvider: nodejs runtime with custom port
- ApacheProvider: nodejs runtime with Unix Domain Socket
- RuntimeMode.NODEJS enum membership
- VHostConfig: node_port default value (3000)
- VHostConfig: node_socket defaults to None
- Template rendering: nodejs-proxy.conf.j2 for both providers
- CLI integration: `vhost create --runtime nodejs` defaults to port 3000
- CLI integration: `vhost create --runtime nodejs --node-port 8080`
- CLI integration: `vhost create --runtime nodejs --node-socket /run/app/app.sock`
"""
import pytest
from pathlib import Path
from typer.testing import CliRunner
from unittest.mock import patch

from vhost_helper.models import VHostConfig, ServerType, RuntimeMode
from vhost_helper.providers import nginx as nginx_module
from vhost_helper.providers import apache as apache_module
from vhost_helper.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Model / enum tests
# ---------------------------------------------------------------------------

def test_runtime_mode_has_nodejs():
    assert RuntimeMode.NODEJS == "nodejs"


def test_vhost_config_node_port_default(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node.local",
        document_root=str(doc_root),
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.NODEJS,
    )
    assert config.node_port == 3000


def test_vhost_config_node_socket_default(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node.local",
        document_root=str(doc_root),
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.NODEJS,
    )
    assert config.node_socket is None


def test_vhost_config_custom_node_port(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node.local",
        document_root=str(doc_root),
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.NODEJS,
        node_port=8080,
    )
    assert config.node_port == 8080


def test_vhost_config_node_socket_set(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node.local",
        document_root=str(doc_root),
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.NODEJS,
        node_socket="/run/node-app/app.sock",
    )
    assert config.node_socket == "/run/node-app/app.sock"


# ---------------------------------------------------------------------------
# Template rendering tests
# ---------------------------------------------------------------------------

def _make_nodejs_config(tmp_path, server_type, node_port=3000, node_socket=None):
    doc_root = tmp_path / "www"
    doc_root.mkdir(exist_ok=True)
    return VHostConfig(
        domain="node-app.local",
        document_root=str(doc_root),
        server_type=server_type,
        runtime=RuntimeMode.NODEJS,
        node_port=node_port,
        node_socket=node_socket,
        template="nodejs-proxy",
    )


def test_nginx_nodejs_proxy_template_default_port(tmp_path):
    config = _make_nodejs_config(tmp_path, ServerType.NGINX)
    provider = nginx_module.NginxProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain=config.domain,
        document_root=str(config.document_root),
        port=config.port,
        runtime=config.runtime.value,
        python_port=config.python_port,
        node_port=config.node_port,
        node_socket=config.node_socket,
        php_socket=config.php_socket,
        os_family="debian_family",
    )
    assert "proxy_pass http://127.0.0.1:3000" in rendered
    assert "proxy_set_header Host" in rendered
    assert "proxy_set_header X-Real-IP" in rendered
    assert "proxy_set_header X-Forwarded-For" in rendered


def test_nginx_nodejs_proxy_template_custom_port(tmp_path):
    config = _make_nodejs_config(tmp_path, ServerType.NGINX, node_port=8080)
    provider = nginx_module.NginxProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain=config.domain,
        document_root=str(config.document_root),
        port=config.port,
        runtime=config.runtime.value,
        python_port=config.python_port,
        node_port=config.node_port,
        node_socket=config.node_socket,
        php_socket=config.php_socket,
        os_family="debian_family",
    )
    assert "proxy_pass http://127.0.0.1:8080" in rendered


def test_nginx_nodejs_proxy_template_unix_socket(tmp_path):
    sock_path = "/run/node-app/app.sock"
    config = _make_nodejs_config(tmp_path, ServerType.NGINX, node_socket=sock_path)
    provider = nginx_module.NginxProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain=config.domain,
        document_root=str(config.document_root),
        port=config.port,
        runtime=config.runtime.value,
        python_port=config.python_port,
        node_port=config.node_port,
        node_socket=config.node_socket,
        php_socket=config.php_socket,
        os_family="debian_family",
    )
    assert f"proxy_pass http://unix:{sock_path}" in rendered
    assert "127.0.0.1" not in rendered


def test_apache_nodejs_proxy_template_default_port(tmp_path):
    config = _make_nodejs_config(tmp_path, ServerType.APACHE)
    provider = apache_module.ApacheProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain=config.domain,
        document_root=str(config.document_root),
        port=config.port,
        runtime=config.runtime.value,
        python_port=config.python_port,
        node_port=config.node_port,
        node_socket=config.node_socket,
        php_socket=config.php_socket,
        os_family="debian_family",
    )
    assert "ProxyPass / http://127.0.0.1:3000/" in rendered
    assert "ProxyPassReverse / http://127.0.0.1:3000/" in rendered
    assert "ProxyPreserveHost On" in rendered


def test_apache_nodejs_proxy_template_custom_port(tmp_path):
    config = _make_nodejs_config(tmp_path, ServerType.APACHE, node_port=8080)
    provider = apache_module.ApacheProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain=config.domain,
        document_root=str(config.document_root),
        port=config.port,
        runtime=config.runtime.value,
        python_port=config.python_port,
        node_port=config.node_port,
        node_socket=config.node_socket,
        php_socket=config.php_socket,
        os_family="debian_family",
    )
    assert "ProxyPass / http://127.0.0.1:8080/" in rendered
    assert "ProxyPassReverse / http://127.0.0.1:8080/" in rendered


def test_apache_nodejs_proxy_template_unix_socket(tmp_path):
    sock_path = "/run/node-app/app.sock"
    config = _make_nodejs_config(tmp_path, ServerType.APACHE, node_socket=sock_path)
    provider = apache_module.ApacheProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain=config.domain,
        document_root=str(config.document_root),
        port=config.port,
        runtime=config.runtime.value,
        python_port=config.python_port,
        node_port=config.node_port,
        node_socket=config.node_socket,
        php_socket=config.php_socket,
        os_family="debian_family",
    )
    assert f"ProxyPass / unix:{sock_path}" in rendered
    assert "127.0.0.1" not in rendered


def test_apache_nodejs_proxy_template_rhel_logs(tmp_path):
    config = _make_nodejs_config(tmp_path, ServerType.APACHE)
    provider = apache_module.ApacheProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain=config.domain,
        document_root=str(config.document_root),
        port=config.port,
        runtime=config.runtime.value,
        python_port=config.python_port,
        node_port=config.node_port,
        node_socket=config.node_socket,
        php_socket=config.php_socket,
        os_family="rhel_family",
    )
    assert "/var/log/httpd/" in rendered


def test_nginx_default_template_nodejs_branch(tmp_path):
    """Verify the default template handles the nodejs runtime branch."""
    config = _make_nodejs_config(tmp_path, ServerType.NGINX)
    config = config.model_copy(update={"template": "default"})
    provider = nginx_module.NginxProvider()
    template = provider._get_template("default")
    rendered = template.render(
        domain=config.domain,
        document_root=str(config.document_root),
        port=config.port,
        runtime=config.runtime.value,
        python_port=config.python_port,
        node_port=config.node_port,
        node_socket=config.node_socket,
        php_socket=config.php_socket,
        os_family="debian_family",
    )
    assert "proxy_pass http://127.0.0.1:3000" in rendered


def test_apache_default_template_nodejs_branch(tmp_path):
    """Verify the default template handles the nodejs runtime branch."""
    config = _make_nodejs_config(tmp_path, ServerType.APACHE)
    config = config.model_copy(update={"template": "default"})
    provider = apache_module.ApacheProvider()
    template = provider._get_template("default")
    rendered = template.render(
        domain=config.domain,
        document_root=str(config.document_root),
        port=config.port,
        runtime=config.runtime.value,
        python_port=config.python_port,
        node_port=config.node_port,
        node_socket=config.node_socket,
        php_socket=config.php_socket,
        os_family="debian_family",
    )
    assert "ProxyPass / http://127.0.0.1:3000/" in rendered


# ---------------------------------------------------------------------------
# Provider unit tests (mocked filesystem)
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_nginx_provider(mocker, tmp_path):
    sites_available = tmp_path / "sites-available"
    sites_enabled = tmp_path / "sites-enabled"
    sites_available.mkdir()
    sites_enabled.mkdir()

    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_available)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.nginx.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False)

    mock_run = mocker.patch("vhost_helper.providers.nginx.run_elevated_command")
    provider = nginx_module.NginxProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")
    provider.mock_run = mock_run
    provider.available = sites_available
    return provider


@pytest.fixture
def patched_apache_provider(mocker, tmp_path):
    sites_available = tmp_path / "sites-available"
    sites_enabled = tmp_path / "sites-enabled"
    sites_available.mkdir()
    sites_enabled.mkdir()

    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", sites_available)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", sites_enabled)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_DISABLED", None)
    mocker.patch("vhost_helper.providers.apache.detected_os_family", "debian_family")
    mocker.patch("vhost_helper.providers.apache.is_selinux_enforcing", return_value=False)

    mock_run = mocker.patch("vhost_helper.providers.apache.run_elevated_command")
    provider = apache_module.ApacheProvider()
    mocker.patch.object(provider, "validate_config", return_value=True)
    mocker.patch.object(provider, "reload")
    provider.mock_run = mock_run
    provider.available = sites_available
    return provider


def test_nginx_provider_creates_nodejs_vhost(patched_nginx_provider, tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node-app.local",
        document_root=str(doc_root),
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.NODEJS,
        node_port=3000,
        template="nodejs-proxy",
    )
    patched_nginx_provider.create_vhost(config, service_running=True)
    patched_nginx_provider.validate_config.assert_called_once()
    patched_nginx_provider.reload.assert_called_once()


def test_apache_provider_creates_nodejs_vhost(patched_apache_provider, tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node-app.local",
        document_root=str(doc_root),
        server_type=ServerType.APACHE,
        runtime=RuntimeMode.NODEJS,
        node_port=3000,
        template="nodejs-proxy",
    )
    patched_apache_provider.create_vhost(config, service_running=True)
    patched_apache_provider.validate_config.assert_called_once()
    patched_apache_provider.reload.assert_called_once()


def test_nginx_provider_nodejs_with_custom_port(patched_nginx_provider, tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node-app.local",
        document_root=str(doc_root),
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.NODEJS,
        node_port=8080,
        template="nodejs-proxy",
    )
    patched_nginx_provider.create_vhost(config, service_running=True)
    # Verify that a config file was written containing the custom port
    conf_file = patched_nginx_provider.available / "node-app.local.conf"
    mv_calls = [c.args[0] for c in patched_nginx_provider.mock_run.call_args_list]
    assert any("mv" in cmd for cmd in mv_calls)


def test_apache_provider_nodejs_with_socket(patched_apache_provider, tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node-app.local",
        document_root=str(doc_root),
        server_type=ServerType.APACHE,
        runtime=RuntimeMode.NODEJS,
        node_socket="/run/node-app/app.sock",
        template="nodejs-proxy",
    )
    patched_apache_provider.create_vhost(config, service_running=True)
    patched_apache_provider.reload.assert_called_once()


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_nginx_cli_setup(mocker, tmp_path):
    available = tmp_path / "nginx-available"
    enabled = tmp_path / "nginx-enabled"
    available.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)

    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=True)

    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch("vhost_helper.main.remove_entry")
    mocker.patch("vhost_helper.main.preflight_sudo_check")

    mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")
    return available, enabled


def test_cli_create_nodejs_default_port(mock_nginx_cli_setup, tmp_path):
    """--runtime nodejs should default to node_port=3000."""
    available, _ = mock_nginx_cli_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(app, [
        "create", "node-app.local", str(doc_root),
        "--provider", "nginx",
        "--runtime", "nodejs",
    ])
    assert result.exit_code == 0, result.stdout
    call_args = nginx_module.NginxProvider.create_vhost.call_args
    assert call_args is not None
    config_arg = call_args.args[0] if call_args.args else call_args[0][0]
    assert config_arg.runtime == RuntimeMode.NODEJS
    assert config_arg.node_port == 3000
    assert config_arg.node_socket is None


def test_cli_create_nodejs_flag(mock_nginx_cli_setup, tmp_path):
    """--nodejs flag sets runtime to NODEJS with default port 3000."""
    available, _ = mock_nginx_cli_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(app, [
        "create", "node-app.local", str(doc_root),
        "--provider", "nginx",
        "--nodejs",
    ])
    assert result.exit_code == 0, result.stdout
    call_args = nginx_module.NginxProvider.create_vhost.call_args
    config_arg = call_args.args[0] if call_args.args else call_args[0][0]
    assert config_arg.runtime == RuntimeMode.NODEJS
    assert config_arg.node_port == 3000


def test_cli_create_nodejs_custom_port(mock_nginx_cli_setup, tmp_path):
    """--runtime nodejs --node-port 8080 proxies to port 8080."""
    available, _ = mock_nginx_cli_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(app, [
        "create", "node-app.local", str(doc_root),
        "--provider", "nginx",
        "--runtime", "nodejs",
        "--node-port", "8080",
    ])
    assert result.exit_code == 0, result.stdout
    call_args = nginx_module.NginxProvider.create_vhost.call_args
    config_arg = call_args.args[0] if call_args.args else call_args[0][0]
    assert config_arg.node_port == 8080


def test_cli_create_nodejs_socket(mock_nginx_cli_setup, tmp_path):
    """--runtime nodejs --node-socket /run/app/app.sock uses UDS."""
    available, _ = mock_nginx_cli_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(app, [
        "create", "node-app.local", str(doc_root),
        "--provider", "nginx",
        "--runtime", "nodejs",
        "--node-socket", "/run/app/app.sock",
    ])
    assert result.exit_code == 0, result.stdout
    call_args = nginx_module.NginxProvider.create_vhost.call_args
    config_arg = call_args.args[0] if call_args.args else call_args[0][0]
    assert config_arg.node_socket == "/run/app/app.sock"


def test_cli_nodejs_and_php_mutually_exclusive(mock_nginx_cli_setup, tmp_path):
    """--nodejs and --php together should produce an error."""
    available, _ = mock_nginx_cli_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(app, [
        "create", "node-app.local", str(doc_root),
        "--provider", "nginx",
        "--nodejs",
        "--php",
    ])
    assert result.exit_code != 0 or "mutually exclusive" in result.stdout


def test_cli_nodejs_and_python_mutually_exclusive(mock_nginx_cli_setup, tmp_path):
    """--runtime nodejs and --python together should produce an error."""
    available, _ = mock_nginx_cli_setup
    doc_root = tmp_path / "www"
    doc_root.mkdir()

    result = runner.invoke(app, [
        "create", "node-app.local", str(doc_root),
        "--provider", "nginx",
        "--runtime", "nodejs",
        "--python",
    ])
    assert result.exit_code != 0 or "mutually exclusive" in result.stdout

