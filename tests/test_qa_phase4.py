"""
QA Phase 4 — Additions for ULTIMATE_VHOST-004: Optional Language Runtimes and
Automated WWW Redirection.

Covers gaps not addressed by test_ultimate_vhost_004.py:
 - BUG-005 regression: validate_config() now handles FileNotFoundError
 - python_port boundary values (model validation)
 - Arch OS family PHP socket path
 - _resolve_php_socket() with arch OS family
 - PHP mode try_files uses /index.php fallback, not /index.html
 - Python mode has no static try_files directive
 - Subdomain redirect logic (sub.example.com → www.sub.example.com)
 - CLI: invalid --python-port exits with code 1
 - CLI: --python-port without --python is silently ignored (static mode)
 - CLI: single-char domain rejected before nginx checks
 - Template: unknown runtime emits no active location block (edge-case guard)
 - VHostConfig: php_socket is stored as provided string
"""
import subprocess
import tempfile
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader
from typer.testing import CliRunner

from vhost_helper.main import app, _resolve_php_socket
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
def doc_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


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


# ---------------------------------------------------------------------------
# BUG-005: validate_config() must handle FileNotFoundError gracefully
# ---------------------------------------------------------------------------

def test_validate_config_returns_false_on_file_not_found(mocker):
    """BUG-005: FileNotFoundError (nginx binary gone after install check) must
    not propagate as an unhandled exception — validate_config should return False."""
    mocker.patch("subprocess.run", side_effect=FileNotFoundError("nginx not found"))
    provider = NginxProvider()
    result = provider.validate_config()
    assert result is False


def test_validate_config_returns_false_on_oserror(mocker):
    """OSError (e.g. permission denied on nginx binary) must return False, not crash."""
    mocker.patch("subprocess.run", side_effect=OSError("permission denied"))
    provider = NginxProvider()
    result = provider.validate_config()
    assert result is False


def test_validate_config_returns_false_on_called_process_error(mocker):
    """Non-zero exit (nginx -t syntax fail) must still return False."""
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=1),
    )
    mocker.patch("vhost_helper.utils._console")
    provider = NginxProvider()
    result = provider.validate_config()
    assert result is False


def test_validate_config_returns_true_on_success(mocker):
    """Sanity: successful subprocess.run returns True."""
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    )
    provider = NginxProvider()
    result = provider.validate_config()
    assert result is True


# ---------------------------------------------------------------------------
# python_port boundary values — model validation
# ---------------------------------------------------------------------------

def test_vhost_config_python_port_min_valid(tmp_path):
    """python_port=1 is the minimum valid value (ge=1)."""
    config = VHostConfig(domain="example.test", document_root=tmp_path, python_port=1)
    assert config.python_port == 1


def test_vhost_config_python_port_max_valid(tmp_path):
    """python_port=65535 is the maximum valid value (le=65535)."""
    config = VHostConfig(domain="example.test", document_root=tmp_path, python_port=65535)
    assert config.python_port == 65535


def test_vhost_config_python_port_zero_rejected(tmp_path):
    """python_port=0 must be rejected by Pydantic (violates ge=1)."""
    with pytest.raises(Exception):  # pydantic.ValidationError
        VHostConfig(domain="example.test", document_root=tmp_path, python_port=0)


def test_vhost_config_python_port_too_large_rejected(tmp_path):
    """python_port=65536 must be rejected by Pydantic (violates le=65535)."""
    with pytest.raises(Exception):
        VHostConfig(domain="example.test", document_root=tmp_path, python_port=65536)


def test_vhost_config_python_port_negative_rejected(tmp_path):
    """Negative python_port must be rejected."""
    with pytest.raises(Exception):
        VHostConfig(domain="example.test", document_root=tmp_path, python_port=-1)


# ---------------------------------------------------------------------------
# Arch OS family PHP socket path
# ---------------------------------------------------------------------------

def test_php_socket_paths_contains_arch_key():
    """PHP_SOCKET_PATHS must have an 'arch' entry."""
    assert "arch" in PHP_SOCKET_PATHS


def test_php_socket_paths_arch_value():
    """Arch PHP socket should be the pacman-standard path."""
    assert PHP_SOCKET_PATHS["arch"] == "/run/php-fpm/php-fpm.sock"


def test_resolve_php_socket_arch_family(mocker):
    """_resolve_php_socket must return the arch socket path when OS family is 'arch'."""
    from vhost_helper.models import OSInfo
    mocker.patch(
        "vhost_helper.main.get_os_info",
        return_value=OSInfo(id="arch", version="rolling", family="arch"),
    )
    result = _resolve_php_socket()
    assert result == PHP_SOCKET_PATHS["arch"]


def test_resolve_php_socket_unknown_family_falls_back_to_default(mocker):
    """Unknown OS family must fall back to DEFAULT_PHP_SOCKET."""
    from vhost_helper.models import OSInfo
    mocker.patch(
        "vhost_helper.main.get_os_info",
        return_value=OSInfo(id="gentoo", version="17.1", family="unknown"),
    )
    result = _resolve_php_socket()
    assert result == DEFAULT_PHP_SOCKET


def test_resolve_php_socket_falls_back_on_os_detection_failure(mocker):
    """If get_os_info() raises, _resolve_php_socket must still return a safe default."""
    mocker.patch("vhost_helper.main.get_os_info", side_effect=RuntimeError("no os"))
    result = _resolve_php_socket()
    assert result == DEFAULT_PHP_SOCKET


def test_php_mode_arch_socket_in_template():
    """Template must render the arch socket path when php_socket is the arch value."""
    arch_socket = PHP_SOCKET_PATHS["arch"]
    config = _render(runtime="php", php_socket=arch_socket)
    assert f"fastcgi_pass unix:{arch_socket}" in config


# ---------------------------------------------------------------------------
# PHP mode: try_files uses /index.php fallback, not /index.html
# ---------------------------------------------------------------------------

def test_php_mode_try_files_uses_php_fallback():
    """PHP mode must use /index.php?$query_string as the try_files fallback."""
    config = _render(runtime="php")
    active_lines = [l for l in config.splitlines() if not l.strip().startswith("#")]
    assert any("try_files" in l and "/index.php" in l for l in active_lines)


def test_php_mode_try_files_does_not_use_html_only_fallback():
    """PHP mode must NOT use /index.html as the sole try_files fallback."""
    config = _render(runtime="php")
    active_lines = [l for l in config.splitlines() if not l.strip().startswith("#")]
    # A line with only /index.html as fallback (no .php) should not appear
    assert not any(
        "try_files" in l and "/index.html" in l and "/index.php" not in l
        for l in active_lines
    )


# ---------------------------------------------------------------------------
# Python mode: no static try_files directive
# ---------------------------------------------------------------------------

def test_python_mode_has_no_try_files_directive():
    """Python mode must NOT emit an active try_files directive (proxy handles routing)."""
    config = _render(runtime="python")
    active_lines = [l for l in config.splitlines() if not l.strip().startswith("#")]
    assert not any("try_files" in l for l in active_lines)


def test_python_mode_does_not_include_fastcgi_params():
    """Python mode must not include fastcgi_params include."""
    config = _render(runtime="python")
    active_lines = [l for l in config.splitlines() if not l.strip().startswith("#")]
    assert not any("fastcgi_params" in l for l in active_lines)


# ---------------------------------------------------------------------------
# Redirect logic: subdomain handling
# ---------------------------------------------------------------------------

def test_subdomain_redirect_block_targets_www_subdomain():
    """sub.example.com (non-www) must generate a redirect from www.sub.example.com."""
    config = _render(domain="sub.example.com")
    assert "server_name www.sub.example.com" in config


def test_www_subdomain_redirect_block_strips_only_www_prefix():
    """www.sub.example.com must redirect from sub.example.com (not www.example.com)."""
    config = _render(domain="www.sub.example.com")
    assert "server_name sub.example.com" in config
    assert "server_name www.example.com" not in config


def test_redirect_server_name_differs_from_main_server_name():
    """The redirect block server_name must not equal the main block server_name."""
    domain = "example.test"
    config = _render(domain=domain)
    # Both server blocks must be present and have different server_names
    assert config.count("server_name") == 2
    assert f"server_name {domain}" in config
    assert f"server_name www.{domain}" in config


def test_redirect_uses_correct_canonical_domain_in_return():
    """The 301 return directive must point to the main domain, not the redirect domain."""
    config = _render(domain="mysite.test")
    # redirect domain is www.mysite.test; return must point to mysite.test
    assert "return 301 $scheme://mysite.test$request_uri" in config


def test_www_redirect_uses_correct_canonical_domain_in_return():
    """For a www. domain, the 301 return must point back to the www. domain."""
    config = _render(domain="www.mysite.test")
    assert "return 301 $scheme://www.mysite.test$request_uri" in config


# ---------------------------------------------------------------------------
# CLI: invalid --python-port exits gracefully
# ---------------------------------------------------------------------------

def test_cli_python_port_zero_exits_with_error(doc_root, mocker):
    """--python-port 0 with --python must exit with code 1 (Pydantic rejects port 0)."""
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    result = runner.invoke(
        app, ["create", "python.test", str(doc_root), "--python", "--python-port", "0"]
    )
    assert result.exit_code == 1


def test_cli_python_port_too_large_exits_with_error(doc_root, mocker):
    """--python-port 65536 with --python must exit with code 1."""
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    result = runner.invoke(
        app, ["create", "python.test", str(doc_root), "--python", "--python-port", "65536"]
    )
    assert result.exit_code == 1


def test_cli_python_port_without_python_flag_uses_static_runtime(doc_root, mocker):
    """--python-port without --python must be accepted; runtime stays static."""
    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.main.add_entry")
    mock_create = mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")

    runner.invoke(app, ["create", "static.test", str(doc_root), "--python-port", "9000"])

    assert mock_create.called
    config_arg: VHostConfig = mock_create.call_args[0][0]
    assert config_arg.runtime == RuntimeMode.STATIC


# ---------------------------------------------------------------------------
# Domain validation edge cases
# ---------------------------------------------------------------------------

def test_single_char_domain_rejected():
    """A single-character domain must be rejected (too short for the regex)."""
    result = runner.invoke(app, ["create", "a", "/tmp"])
    assert result.exit_code == 1
    assert "Invalid domain format" in result.output


def test_two_char_domain_rejected():
    """A two-character domain must be rejected (middle group requires 1+ char)."""
    result = runner.invoke(app, ["create", "ab", "/tmp"])
    assert result.exit_code == 1
    assert "Invalid domain format" in result.output


def test_domain_starting_with_dot_rejected():
    """A domain starting with a dot must be rejected."""
    result = runner.invoke(app, ["create", "--", ".example.test", "/tmp"])
    assert result.exit_code == 1


def test_domain_ending_with_dot_rejected():
    """A domain ending with a dot must be rejected."""
    result = runner.invoke(app, ["create", "example.", "/tmp"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Template: unknown runtime produces no active location block
# ---------------------------------------------------------------------------

def test_unknown_runtime_produces_no_active_fastcgi_or_proxy():
    """An unknown runtime string must not emit any active fastcgi_pass or proxy_pass."""
    config = _render(runtime="ruby")
    active_lines = [l for l in config.splitlines() if not l.strip().startswith("#")]
    assert not any("fastcgi_pass" in l for l in active_lines)
    assert not any("proxy_pass" in l for l in active_lines)


def test_unknown_runtime_still_produces_two_server_blocks():
    """Even with an unknown runtime, both server blocks (main + redirect) must exist."""
    config = _render(runtime="ruby")
    assert config.count("server {") == 2


# ---------------------------------------------------------------------------
# VHostConfig: php_socket stored correctly
# ---------------------------------------------------------------------------

def test_vhost_config_php_socket_stored_as_provided(tmp_path):
    """php_socket must be stored exactly as supplied."""
    custom_socket = "/custom/path/php-fpm.sock"
    config = VHostConfig(
        domain="example.test",
        document_root=tmp_path,
        php_socket=custom_socket,
    )
    assert config.php_socket == custom_socket


def test_vhost_config_rhel_php_socket_stored(tmp_path):
    """RHEL php socket path must be accepted as a valid string."""
    rhel_socket = PHP_SOCKET_PATHS["rhel"]
    config = VHostConfig(
        domain="example.test",
        document_root=tmp_path,
        php_socket=rhel_socket,
    )
    assert config.php_socket == rhel_socket


# ---------------------------------------------------------------------------
# Template: port is rendered into both server blocks
# ---------------------------------------------------------------------------

def test_template_custom_port_appears_in_main_server_block():
    """A non-default port must appear in the main server block listen directive."""
    config = _render(port=8080)
    lines_with_listen = [l for l in config.splitlines() if "listen" in l]
    assert any("8080" in l for l in lines_with_listen)


def test_template_custom_port_appears_in_redirect_block():
    """A non-default port must also appear in the redirect server block."""
    config = _render(port=8080)
    # Both server blocks use the same port
    assert config.count("listen 8080") == 2


# ---------------------------------------------------------------------------
# Template: document_root quoted correctly
# ---------------------------------------------------------------------------

def test_template_document_root_with_spaces_renders():
    """document_root with spaces must be quoted in the template output."""
    config = _render(document_root="/var/www/my project")
    assert 'root "/var/www/my project"' in config
