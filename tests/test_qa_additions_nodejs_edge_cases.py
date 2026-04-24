"""
QA additions for Node.js runtime support — edge cases, boundary values,
and negative tests for ULTIMATE_VHOST-018.
"""

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from vhost_helper.models import VHostConfig, ServerType, RuntimeMode
from vhost_helper.providers import nginx as nginx_module
from vhost_helper.providers import apache as apache_module
from vhost_helper.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Boundary / edge cases on VHostConfig node_port
# ---------------------------------------------------------------------------


def test_node_port_boundary_min(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node.local",
        document_root=str(doc_root),
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.NODEJS,
        node_port=1,
    )
    assert config.node_port == 1


def test_node_port_boundary_max(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node.local",
        document_root=str(doc_root),
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.NODEJS,
        node_port=65535,
    )
    assert config.node_port == 65535


def test_node_port_zero_rejected(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    with pytest.raises(ValidationError):
        VHostConfig(
            domain="node.local",
            document_root=str(doc_root),
            server_type=ServerType.NGINX,
            runtime=RuntimeMode.NODEJS,
            node_port=0,
        )


def test_node_port_above_max_rejected(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    with pytest.raises(ValidationError):
        VHostConfig(
            domain="node.local",
            document_root=str(doc_root),
            server_type=ServerType.NGINX,
            runtime=RuntimeMode.NODEJS,
            node_port=65536,
        )


def test_node_port_negative_rejected(tmp_path):
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    with pytest.raises(ValidationError):
        VHostConfig(
            domain="node.local",
            document_root=str(doc_root),
            server_type=ServerType.NGINX,
            runtime=RuntimeMode.NODEJS,
            node_port=-1,
        )


# ---------------------------------------------------------------------------
# node_socket edge cases
# ---------------------------------------------------------------------------


def test_node_socket_empty_string(tmp_path):
    """An empty string node_socket should be treated as falsy (use port instead)."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    config = VHostConfig(
        domain="node.local",
        document_root=str(doc_root),
        server_type=ServerType.NGINX,
        runtime=RuntimeMode.NODEJS,
        node_socket="",
    )
    # Empty string is stored; templates must treat it as falsy
    provider = nginx_module.NginxProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain=config.domain,
        document_root=str(config.document_root),
        port=config.port,
        runtime=config.runtime.value,
        node_port=config.node_port,
        node_socket=config.node_socket,
        php_socket=config.php_socket,
        python_port=config.python_port,
        os_family="debian_family",
    )
    # Empty string is falsy in Jinja2, so should fall back to port
    assert f"proxy_pass http://127.0.0.1:{config.node_port}" in rendered


def test_node_socket_with_spaces_in_path(tmp_path):
    """Paths with spaces are unusual but must not crash template rendering."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    socket_path = "/run/my app/app.sock"
    provider = nginx_module.NginxProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain="node.local",
        document_root=str(doc_root),
        port=80,
        runtime="nodejs",
        node_port=3000,
        node_socket=socket_path,
        php_socket="/run/php/php-fpm.sock",
        python_port=8000,
        os_family="debian_family",
    )
    assert socket_path in rendered


# ---------------------------------------------------------------------------
# Template rendering — RHEL vs Debian log paths
# ---------------------------------------------------------------------------


def test_nginx_nodejs_proxy_uses_fixed_log_path(tmp_path):
    """Nginx log path is always /var/log/nginx (no distro branching)."""
    provider = nginx_module.NginxProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain="node.local",
        document_root=str(tmp_path),
        port=80,
        runtime="nodejs",
        node_port=3000,
        node_socket=None,
        php_socket="/run/php/php-fpm.sock",
        python_port=8000,
        os_family="rhel_family",
    )
    assert "/var/log/nginx/" in rendered


def test_apache_nodejs_proxy_debian_log_path(tmp_path):
    provider = apache_module.ApacheProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain="node.local",
        document_root=str(tmp_path),
        port=80,
        runtime="nodejs",
        node_port=3000,
        node_socket=None,
        php_socket="/run/php/php-fpm.sock",
        python_port=8000,
        os_family="debian_family",
    )
    assert "${APACHE_LOG_DIR}" in rendered
    assert "/var/log/httpd/" not in rendered


def test_apache_nodejs_proxy_rhel_log_path(tmp_path):
    provider = apache_module.ApacheProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain="node.local",
        document_root=str(tmp_path),
        port=80,
        runtime="nodejs",
        node_port=3000,
        node_socket=None,
        php_socket="/run/php/php-fpm.sock",
        python_port=8000,
        os_family="rhel_family",
    )
    assert "/var/log/httpd/" in rendered
    assert "${APACHE_LOG_DIR}" not in rendered


# ---------------------------------------------------------------------------
# Template rendering — canonical redirect with custom port
# ---------------------------------------------------------------------------


def test_nginx_nodejs_canonical_redirect_custom_port(tmp_path):
    """Redirect block must include port when it is not 80 or 443."""
    provider = nginx_module.NginxProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain="node.local",
        document_root=str(tmp_path),
        port=8888,
        runtime="nodejs",
        node_port=3000,
        node_socket=None,
        php_socket="/run/php/php-fpm.sock",
        python_port=8000,
        os_family="debian_family",
    )
    assert ":8888" in rendered


def test_nginx_nodejs_canonical_redirect_no_port_80(tmp_path):
    """Redirect block must NOT include port 80."""
    provider = nginx_module.NginxProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain="node.local",
        document_root=str(tmp_path),
        port=80,
        runtime="nodejs",
        node_port=3000,
        node_socket=None,
        php_socket="/run/php/php-fpm.sock",
        python_port=8000,
        os_family="debian_family",
    )
    assert "return 301 $scheme://node.local$request_uri" in rendered


def test_apache_nodejs_redirect_www_to_bare(tmp_path):
    """www. domain should redirect to bare domain."""
    provider = apache_module.ApacheProvider()
    template = provider._get_template("nodejs-proxy")
    rendered = template.render(
        domain="www.node.local",
        document_root=str(tmp_path),
        port=80,
        runtime="nodejs",
        node_port=3000,
        node_socket=None,
        php_socket="/run/php/php-fpm.sock",
        python_port=8000,
        os_family="debian_family",
    )
    assert "node.local" in rendered
    # The redirect domain should be the bare domain
    assert "ServerName node.local" in rendered


# ---------------------------------------------------------------------------
# Apache UDS format
# ---------------------------------------------------------------------------


def test_apache_nodejs_uds_format_includes_pipe(tmp_path):
    """Apache UDS proxy syntax uses pipe: unix:/path|http://localhost/."""
    provider = apache_module.ApacheProvider()
    template = provider._get_template("nodejs-proxy")
    sock = "/run/node-app/app.sock"
    rendered = template.render(
        domain="node.local",
        document_root=str(tmp_path),
        port=80,
        runtime="nodejs",
        node_port=3000,
        node_socket=sock,
        php_socket="/run/php/php-fpm.sock",
        python_port=8000,
        os_family="debian_family",
    )
    assert f"unix:{sock}|http://localhost/" in rendered


# ---------------------------------------------------------------------------
# CLI edge cases
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nginx_setup(mocker, tmp_path):
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


def test_cli_node_port_without_nodejs_runtime_ignored(mock_nginx_setup, tmp_path):
    """--node-port without --runtime nodejs should not set nodejs runtime."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    result = runner.invoke(
        app,
        [
            "create",
            "site.local",
            str(doc_root),
            "--provider",
            "nginx",
            "--node-port",
            "9000",
        ],
    )
    assert result.exit_code == 0, result.stdout
    call_args = nginx_module.NginxProvider.create_vhost.call_args
    config_arg = call_args.args[0] if call_args.args else call_args[0][0]
    # Runtime defaults to static when --nodejs is not specified
    assert config_arg.runtime == RuntimeMode.STATIC
    # node_port is still stored but won't be used in template
    assert config_arg.node_port == 9000


def test_cli_node_socket_without_runtime_nodejs(mock_nginx_setup, tmp_path):
    """--node-socket alone (without --nodejs) stores socket but keeps static runtime."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    result = runner.invoke(
        app,
        [
            "create",
            "site.local",
            str(doc_root),
            "--provider",
            "nginx",
            "--node-socket",
            "/run/app/app.sock",
        ],
    )
    assert result.exit_code == 0, result.stdout
    call_args = nginx_module.NginxProvider.create_vhost.call_args
    config_arg = call_args.args[0] if call_args.args else call_args[0][0]
    assert config_arg.runtime == RuntimeMode.STATIC
    assert config_arg.node_socket == "/run/app/app.sock"


def test_cli_nodejs_runtime_option_same_as_flag(mock_nginx_setup, tmp_path):
    """--runtime nodejs and --nodejs should both produce RuntimeMode.NODEJS."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    result_flag = runner.invoke(
        app,
        [
            "create",
            "node-a.local",
            str(doc_root),
            "--provider",
            "nginx",
            "--nodejs",
        ],
    )
    assert result_flag.exit_code == 0
    call_flag = nginx_module.NginxProvider.create_vhost.call_args.args[0]

    result_opt = runner.invoke(
        app,
        [
            "create",
            "node-b.local",
            str(doc_root),
            "--provider",
            "nginx",
            "--runtime",
            "nodejs",
        ],
    )
    assert result_opt.exit_code == 0
    call_opt = nginx_module.NginxProvider.create_vhost.call_args.args[0]

    assert call_flag.runtime == RuntimeMode.NODEJS
    assert call_opt.runtime == RuntimeMode.NODEJS
    assert call_flag.node_port == 3000
    assert call_opt.node_port == 3000


def test_cli_all_three_runtime_flags_mutually_exclusive(mock_nginx_setup, tmp_path):
    """--php, --python, and --nodejs together should fail."""
    doc_root = tmp_path / "www"
    doc_root.mkdir()
    result = runner.invoke(
        app,
        [
            "create",
            "node.local",
            str(doc_root),
            "--provider",
            "nginx",
            "--php",
            "__auto__",
            "--python",
            "--nodejs",
        ],
    )
    assert result.exit_code != 0 or "mutually exclusive" in result.stdout


# ---------------------------------------------------------------------------
# templates sub-app edge cases
# ---------------------------------------------------------------------------


def test_templates_list_exits_zero():
    result = runner.invoke(app, ["templates", "list"])
    assert result.exit_code == 0, result.output


def test_templates_list_includes_nodejs_proxy():
    result = runner.invoke(app, ["templates", "list"])
    assert result.exit_code == 0
    assert "nodejs-proxy" in result.output


def test_templates_inspect_nodejs_proxy_nginx():
    result = runner.invoke(app, ["templates", "inspect", "nginx-nodejs-proxy"])
    assert result.exit_code == 0, result.output
    assert "node_port" in result.output
    assert "node_socket" in result.output


def test_templates_inspect_nodejs_proxy_apache():
    result = runner.invoke(app, ["templates", "inspect", "apache-nodejs-proxy"])
    assert result.exit_code == 0, result.output
    assert "node_port" in result.output


def test_templates_inspect_unknown_exits_one():
    result = runner.invoke(app, ["templates", "inspect", "nginx-grpc"])
    assert result.exit_code == 1


def test_templates_inspect_shows_em_dash_for_no_default():
    """Variables without a default must show — (em-dash)."""
    result = runner.invoke(app, ["templates", "inspect", "nginx-nodejs-proxy"])
    assert result.exit_code == 0
    assert "—" in result.output


def test_templates_list_filter_nginx_only():
    result = runner.invoke(app, ["templates", "list", "--provider", "nginx"])
    assert result.exit_code == 0
    assert "apache" not in result.output.lower()


def test_templates_list_filter_apache_only():
    result = runner.invoke(app, ["templates", "list", "--provider", "apache"])
    assert result.exit_code == 0
    assert "nginx" not in result.output.lower()


def test_templates_list_unknown_provider_exits_nonzero():
    result = runner.invoke(app, ["templates", "list", "--provider", "lighttpd"])
    assert result.exit_code != 0
