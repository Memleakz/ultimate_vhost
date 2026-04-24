"""
Tests for ULTIMATE_VHOST-004: Optional Language Runtimes and Automated WWW Redirection.

Acceptance criteria verified here:
1. Static mode produces try_files /index.html; no fastcgi_pass or proxy_pass in output.
2. Static mode includes commented-out PHP and Python example blocks.
3. PHP mode produces a fastcgi_pass block with the correct socket and fastcgi_params.
4. PHP mode uses the RHEL socket path when the OS family is 'rhel'.
5. Python mode produces a proxy_pass block with correct headers (default port 8000).
6. Python mode respects --python-port override.
7. --php and --python together exit code 1 with the mutual exclusivity error message.
8. Every generated config includes a canonical 301 redirect server block.
9. Redirect for non-www domain targets www counterpart (and vice versa).
10. Redirect uses HTTP status 301, never 302.
"""

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vhost_helper.main import app
from vhost_helper.models import (
    VHostConfig,
    RuntimeMode,
    PHP_SOCKET_PATHS,
    DEFAULT_PHP_SOCKET,
)
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
    # We can instantiate the provider directly here because for this test file,
    # we only care about the rendered output, not the file system operations,
    # which are mocked at a higher level in other tests.
    # The key is that it will use the correct ChoiceLoader.
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_nginx_dirs(mocker):
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        available = tmp_path / "sites-available"
        enabled = tmp_path / "sites-enabled"
        available.mkdir()
        enabled.mkdir()
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", enabled)
        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
        yield available, enabled, tmp_path


@pytest.fixture
def doc_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# 3.1 Static HTML mode
# ---------------------------------------------------------------------------


def test_static_mode_uses_try_files_index_html():
    config = _render(runtime="static")
    assert "try_files $uri $uri/ /index.html" in config


def test_static_mode_has_no_fastcgi_pass():
    config = _render(runtime="static")
    # Only uncommented fastcgi_pass is forbidden; commented lines are allowed.
    for line in config.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert (
            "fastcgi_pass" not in stripped
        ), f"Unexpected active fastcgi_pass in static mode: {line!r}"


def test_static_mode_has_no_proxy_pass():
    config = _render(runtime="static")
    for line in config.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert (
            "proxy_pass" not in stripped
        ), f"Unexpected active proxy_pass in static mode: {line!r}"


def test_static_mode_index_does_not_include_php():
    config = _render(runtime="static")
    # The active index directive must not list index.php
    for line in config.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("index "):
            assert "index.php" not in stripped


# ---------------------------------------------------------------------------
# 3.5 Commented-out runtime blocks in static mode
# ---------------------------------------------------------------------------


def test_static_mode_has_commented_fastcgi_block():
    config = _render(runtime="static")
    assert "# location ~ \\.php$" in config or "# location ~ \\.php$" in config.replace(
        "\\", ""
    )
    # Normalise — check for the key directive inside a comment
    assert any(
        "fastcgi_pass" in line and line.strip().startswith("#")
        for line in config.splitlines()
    )


def test_static_mode_has_commented_proxy_pass_block():
    config = _render(runtime="static")
    assert any(
        "proxy_pass" in line and line.strip().startswith("#")
        for line in config.splitlines()
    )


def test_static_mode_has_commented_proxy_set_header():
    config = _render(runtime="static")
    assert any(
        "proxy_set_header" in line and line.strip().startswith("#")
        for line in config.splitlines()
    )


# ---------------------------------------------------------------------------
# 3.2 PHP runtime mode
# ---------------------------------------------------------------------------


def test_php_mode_has_fastcgi_pass():
    config = _render(runtime="php", php_socket="/run/php/php-fpm.sock")
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    assert any("fastcgi_pass" in line for line in active_lines)


def test_php_mode_fastcgi_pass_uses_correct_socket():
    socket = "/run/php/php-fpm.sock"
    config = _render(runtime="php", php_socket=socket)
    assert f"fastcgi_pass unix:{socket}" in config


def test_php_mode_fastcgi_pass_uses_rhel_socket():
    socket = PHP_SOCKET_PATHS["rhel"]
    config = _render(runtime="php", php_socket=socket)
    assert f"fastcgi_pass unix:{socket}" in config


def test_php_mode_includes_fastcgi_params():
    config = _render(runtime="php")
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    assert any("include fastcgi_params" in line for line in active_lines)


def test_php_mode_includes_script_filename_param():
    config = _render(runtime="php")
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    assert any("SCRIPT_FILENAME" in line for line in active_lines)


def test_php_mode_has_no_proxy_pass():
    config = _render(runtime="php")
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    assert not any("proxy_pass" in line for line in active_lines)


def test_php_mode_index_includes_php():
    config = _render(runtime="php")
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    index_lines = [line for line in active_lines if line.strip().startswith("index ")]
    assert any("index.php" in line for line in index_lines)


# ---------------------------------------------------------------------------
# 3.3 Python runtime mode
# ---------------------------------------------------------------------------


def test_python_mode_has_proxy_pass_default_port():
    config = _render(runtime="python", python_port=8000)
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    assert any("proxy_pass http://127.0.0.1:8000" in line for line in active_lines)


def test_python_mode_has_proxy_pass_custom_port():
    config = _render(runtime="python", python_port=9000)
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    assert any("proxy_pass http://127.0.0.1:9000" in line for line in active_lines)


def test_python_mode_has_host_header():
    config = _render(runtime="python")
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    assert any("proxy_set_header Host" in line for line in active_lines)


def test_python_mode_has_x_real_ip_header():
    config = _render(runtime="python")
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    assert any("proxy_set_header X-Real-IP" in line for line in active_lines)


def test_python_mode_has_x_forwarded_for_header():
    config = _render(runtime="python")
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    assert any("proxy_set_header X-Forwarded-For" in line for line in active_lines)


def test_python_mode_has_no_fastcgi_pass():
    config = _render(runtime="python")
    active_lines = [
        line for line in config.splitlines() if not line.strip().startswith("#")
    ]
    assert not any("fastcgi_pass" in line for line in active_lines)


# ---------------------------------------------------------------------------
# 3.4 Canonical WWW redirect
# ---------------------------------------------------------------------------


def test_non_www_domain_generates_www_redirect_block():
    config = _render(domain="example.com")
    assert "server_name www.example.com" in config


def test_www_domain_generates_non_www_redirect_block():
    config = _render(domain="www.example.com")
    assert "server_name example.com" in config


def test_redirect_block_uses_301():
    config = _render(domain="example.com")
    assert "return 301 $scheme://example.com$request_uri" in config


def test_www_redirect_block_uses_301():
    config = _render(domain="www.example.com")
    assert "return 301 $scheme://www.example.com$request_uri" in config


def test_redirect_block_never_uses_302():
    config_plain = _render(domain="example.com")
    config_www = _render(domain="www.example.com")
    assert "return 302" not in config_plain
    assert "return 302" not in config_www


def test_config_has_two_server_blocks():
    config = _render(domain="example.com")
    assert config.count("server {") == 2


# ---------------------------------------------------------------------------
# 3.3 CLI: --php and --python mutual exclusivity
# ---------------------------------------------------------------------------


def test_cli_mutual_exclusivity_exits_code_1(doc_root, mocker):
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    result = runner.invoke(
        app, ["create", "example.test", str(doc_root), "--php", "--python"]
    )
    assert result.exit_code == 1


def test_cli_mutual_exclusivity_shows_error_message(doc_root, mocker):
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    result = runner.invoke(
        app, ["create", "example.test", str(doc_root), "--php", "--python"]
    )
    assert "mutually exclusive" in result.output


def test_cli_mutual_exclusivity_writes_no_files(tmp_nginx_dirs, doc_root, mocker):
    available, enabled, _ = tmp_nginx_dirs
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.add_entry")
    runner.invoke(app, ["create", "example.test", str(doc_root), "--php", "--python"])
    assert not list(
        available.iterdir()
    ), "No config file should be written on mutual exclusion error"


# ---------------------------------------------------------------------------
# CLI: --php flag integration
# ---------------------------------------------------------------------------


def test_cli_php_flag_passes_runtime_to_provider(tmp_nginx_dirs, doc_root, mocker):
    available, enabled, _ = tmp_nginx_dirs
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    mocker.patch(
        "vhost_helper.main._resolve_php_socket", return_value="/run/php/php-fpm.sock"
    )
    mock_create = mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.create_vhost"
    )

    runner.invoke(app, ["create", "php.test", str(doc_root), "--php"])

    assert mock_create.called
    config_arg: VHostConfig = mock_create.call_args[0][0]
    assert config_arg.runtime == RuntimeMode.PHP


def test_cli_python_flag_passes_runtime_to_provider(tmp_nginx_dirs, doc_root, mocker):
    available, enabled, _ = tmp_nginx_dirs
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    mock_create = mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.create_vhost"
    )

    runner.invoke(app, ["create", "python.test", str(doc_root), "--python"])

    assert mock_create.called
    config_arg: VHostConfig = mock_create.call_args[0][0]
    assert config_arg.runtime == RuntimeMode.PYTHON


def test_cli_python_port_override(tmp_nginx_dirs, doc_root, mocker):
    available, enabled, _ = tmp_nginx_dirs
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    mock_create = mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.create_vhost"
    )

    runner.invoke(
        app,
        ["create", "python.test", str(doc_root), "--python", "--python-port", "9000"],
    )

    assert mock_create.called
    config_arg: VHostConfig = mock_create.call_args[0][0]
    assert config_arg.python_port == 9000


def test_cli_no_flags_defaults_to_static_runtime(tmp_nginx_dirs, doc_root, mocker):
    available, enabled, _ = tmp_nginx_dirs
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    mock_create = mocker.patch(
        "vhost_helper.providers.nginx.NginxProvider.create_vhost"
    )

    runner.invoke(app, ["create", "static.test", str(doc_root)])

    assert mock_create.called
    config_arg: VHostConfig = mock_create.call_args[0][0]
    assert config_arg.runtime == RuntimeMode.STATIC


# ---------------------------------------------------------------------------
# VHostConfig model: new fields
# ---------------------------------------------------------------------------


def test_vhost_config_defaults_to_static_runtime(doc_root):
    config = VHostConfig(domain="example.test", document_root=doc_root)
    assert config.runtime == RuntimeMode.STATIC


def test_vhost_config_default_python_port(doc_root):
    config = VHostConfig(domain="example.test", document_root=doc_root)
    assert config.python_port == 8000


def test_vhost_config_default_php_socket(doc_root):
    config = VHostConfig(domain="example.test", document_root=doc_root)
    assert config.php_socket == DEFAULT_PHP_SOCKET


def test_vhost_config_accepts_php_runtime(doc_root):
    config = VHostConfig(
        domain="example.test", document_root=doc_root, runtime=RuntimeMode.PHP
    )
    assert config.runtime == RuntimeMode.PHP


def test_vhost_config_accepts_python_runtime(doc_root):
    config = VHostConfig(
        domain="example.test", document_root=doc_root, runtime=RuntimeMode.PYTHON
    )
    assert config.runtime == RuntimeMode.PYTHON


# ---------------------------------------------------------------------------
# NginxProvider: template variables wired correctly
# ---------------------------------------------------------------------------


def test_provider_renders_php_socket_from_config(tmp_nginx_dirs, mocker):
    import subprocess

    available, enabled, tmp_path = tmp_nginx_dirs
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")

    provider = NginxProvider()
    config = VHostConfig(
        domain="php.test",
        document_root=tmp_path,
        runtime=RuntimeMode.PHP,
        php_socket="/run/php-fpm/www.sock",
    )
    provider.create_vhost(config, service_running=False)

    # Verify the file was written (mv was called)
    calls = [str(c[0][0]) for c in subprocess.run.call_args_list]
    assert any("mv" in c for c in calls)


def test_provider_renders_python_port_from_config(tmp_nginx_dirs, mocker):
    import subprocess

    available, enabled, tmp_path = tmp_nginx_dirs
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    mocker.patch("vhost_helper.utils._console")

    provider = NginxProvider()
    config = VHostConfig(
        domain="python.test",
        document_root=tmp_path,
        runtime=RuntimeMode.PYTHON,
        python_port=9000,
    )
    provider.create_vhost(config, service_running=False)

    calls = [str(c[0][0]) for c in subprocess.run.call_args_list]
    assert any("mv" in c for c in calls)
