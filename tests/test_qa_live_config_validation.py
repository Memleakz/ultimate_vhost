"""
Live configuration syntax validation tests — ULTIMATE_VHOST-018
PRD §3.3 AC-4 / AC-5: Generated Nginx and Apache configs MUST pass their
respective native syntax-check utilities (`nginx -t` and `httpd -t`).

These tests render actual Jinja2 templates and feed the output to the
binary's syntax checker, providing end-to-end evidence that the generated
configuration is valid beyond what any mock-based test can guarantee.

Requirements:
  - `nginx` binary present in PATH (nginx -t).
  - `httpd` binary present in PATH (httpd -t).

Tests are skipped (not failed) when the required binary is absent so the
suite remains green in minimal environments that only have one server installed.
"""

import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Resolve the templates directory relative to this test file so the suite
# works both locally and after `install.sh` global deployment.
_TESTS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _TESTS_DIR.parent
_TEMPLATES_DIR = _SRC_DIR / "templates"


def _render(provider: str, template_name: str, **ctx) -> str:
    """Render a .conf.j2 template from src/templates/<provider>/."""
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR / provider)))
    tmpl = env.get_template(f"{template_name}.conf.j2")
    return tmpl.render(**ctx)


def _nginx_test(config_fragment: str) -> tuple[bool, str]:
    """
    Wrap *config_fragment* in a minimal, non-root-safe nginx http{} block,
    write it to a temp file, and run `nginx -t`.

    Returns (passed: bool, output: str).
    Uses high port (8080+) and /tmp paths so no root privilege is needed.
    """
    tmpdir = tempfile.mkdtemp(prefix="vhost_nginx_test_")
    try:
        for d in ["client_body", "proxy", "fastcgi", "uwsgi", "scgi"]:
            (Path(tmpdir) / d).mkdir()

        conf_path = Path(tmpdir) / "test.conf"
        pid_path = Path(tmpdir) / "nginx.pid"
        access_log = Path(tmpdir) / "access.log"
        error_log = Path(tmpdir) / "error.log"

        # Replace any /var/log/nginx paths in the fragment with writable tmp paths
        safe_fragment = config_fragment.replace("/var/log/nginx/", str(tmpdir) + "/")
        # Replace port 80 listen directives with 8080 to avoid permission errors
        safe_fragment = safe_fragment.replace("listen 80;", "listen 8080;")

        full_conf = textwrap.dedent(f"""\
            pid {pid_path};
            error_log {error_log};
            events {{}}
            http {{
                access_log {access_log};
                client_body_temp_path {tmpdir}/client_body;
                proxy_temp_path {tmpdir}/proxy;
                fastcgi_temp_path {tmpdir}/fastcgi;
                uwsgi_temp_path {tmpdir}/uwsgi;
                scgi_temp_path {tmpdir}/scgi;
            {textwrap.indent(safe_fragment, '    ')}
            }}
        """)
        conf_path.write_text(full_conf)

        result = subprocess.run(
            ["nginx", "-t", "-c", str(conf_path)],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
        # nginx -t exits 0 only when syntax is OK AND pid file can be written.
        # The "syntax is ok" message is the reliable indicator in our non-root env.
        passed = (
            "syntax is ok" in output
            and "configuration file" in output
            and ("is successful" in output or result.returncode == 0)
        )
        return passed, output
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _httpd_test(config_fragment: str) -> tuple[bool, str]:
    """
    Wrap *config_fragment* in a minimal httpd configuration (with required
    modules loaded) and run `httpd -t`.

    Returns (passed: bool, output: str).
    """
    tmpdir = tempfile.mkdtemp(prefix="vhost_httpd_test_")
    try:
        pid_path = Path(tmpdir) / "httpd.pid"
        error_log = Path(tmpdir) / "error.log"
        Path(tmpdir) / "access.log"

        mod_dir = "/usr/lib64/httpd/modules"

        # Replace any unwritable log paths (${APACHE_LOG_DIR} or /var/log/httpd/)
        safe_fragment = config_fragment.replace(
            "${APACHE_LOG_DIR}/", str(tmpdir) + "/"
        ).replace("/var/log/httpd/", str(tmpdir) + "/")
        # Replace port 80 to avoid permission errors
        safe_fragment = safe_fragment.replace("*:80>", "*:8081>")

        full_conf = textwrap.dedent(f"""\
            ServerRoot "/etc/httpd"
            PidFile {pid_path}
            ServerName localhost
            ErrorLog {error_log}

            LoadModule mpm_event_module {mod_dir}/mod_mpm_event.so
            LoadModule unixd_module {mod_dir}/mod_unixd.so
            LoadModule log_config_module {mod_dir}/mod_log_config.so
            LoadModule proxy_module {mod_dir}/mod_proxy.so
            LoadModule proxy_http_module {mod_dir}/mod_proxy_http.so
            LoadModule headers_module {mod_dir}/mod_headers.so
            LoadModule alias_module {mod_dir}/mod_alias.so

            Listen 8081

            {config_fragment.replace("${APACHE_LOG_DIR}/", str(tmpdir) + "/")
                             .replace("/var/log/httpd/", str(tmpdir) + "/")
                             .replace("*:80>", "*:8081>")}
        """)
        conf_path = Path(tmpdir) / "test.conf"
        conf_path.write_text(full_conf)

        result = subprocess.run(
            ["httpd", "-t", "-f", str(conf_path)],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
        passed = "Syntax OK" in output and result.returncode == 0
        return passed, output
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Skip markers
# ---------------------------------------------------------------------------

requires_nginx = pytest.mark.skipif(
    shutil.which("nginx") is None,
    reason="nginx binary not found in PATH",
)
requires_httpd = pytest.mark.skipif(
    shutil.which("httpd") is None,
    reason="httpd binary not found in PATH",
)


# ===========================================================================
# Nginx — nodejs-proxy template (PRD §3.3 AC-4)
# ===========================================================================


class TestNginxNodejsLiveSyntax:
    """AC-4: Generated Nginx nodejs-proxy configuration passes `nginx -t`."""

    @requires_nginx
    def test_nginx_nodejs_default_port_passes_syntax_check(self):
        """Default port 3000 — TCP proxy variant."""
        rendered = _render(
            "nginx",
            "nodejs-proxy",
            domain="node-app.local",
            port=80,
            node_port=3000,
            node_socket=None,
        )
        passed, output = _nginx_test(rendered)
        assert (
            passed
        ), f"nginx -t FAILED.\nRendered config fragment:\n{rendered}\nnginx output:\n{output}"

    @requires_nginx
    def test_nginx_nodejs_custom_port_passes_syntax_check(self):
        """Custom port 8080 — TCP proxy variant."""
        rendered = _render(
            "nginx",
            "nodejs-proxy",
            domain="node-app.local",
            port=80,
            node_port=8080,
            node_socket=None,
        )
        passed, output = _nginx_test(rendered)
        assert (
            passed
        ), f"nginx -t FAILED.\nRendered config fragment:\n{rendered}\nnginx output:\n{output}"

    @requires_nginx
    def test_nginx_nodejs_unix_socket_passes_syntax_check(self):
        """Unix Domain Socket proxy variant."""
        rendered = _render(
            "nginx",
            "nodejs-proxy",
            domain="node-app.local",
            port=80,
            node_port=3000,
            node_socket="/run/node-app/app.sock",
        )
        passed, output = _nginx_test(rendered)
        assert (
            passed
        ), f"nginx -t FAILED.\nRendered config fragment:\n{rendered}\nnginx output:\n{output}"

    @requires_nginx
    def test_nginx_nodejs_www_domain_redirect_passes_syntax_check(self):
        """www.* domain — canonical redirect direction reversed."""
        rendered = _render(
            "nginx",
            "nodejs-proxy",
            domain="www.node-app.local",
            port=80,
            node_port=3000,
            node_socket=None,
        )
        passed, output = _nginx_test(rendered)
        assert (
            passed
        ), f"nginx -t FAILED.\nRendered config fragment:\n{rendered}\nnginx output:\n{output}"

    @requires_nginx
    def test_nginx_nodejs_non_standard_port_passes_syntax_check(self):
        """Non-standard listen port — canonical redirect should include port."""
        rendered = _render(
            "nginx",
            "nodejs-proxy",
            domain="node-app.local",
            port=8443,
            node_port=3000,
            node_socket=None,
        )
        passed, output = _nginx_test(rendered)
        assert (
            passed
        ), f"nginx -t FAILED.\nRendered config fragment:\n{rendered}\nnginx output:\n{output}"


# ===========================================================================
# Apache — nodejs-proxy template (PRD §3.3 AC-5)
# ===========================================================================


class TestApacheNodejsLiveSyntax:
    """AC-5: Generated Apache nodejs-proxy configuration passes `httpd -t`."""

    @requires_httpd
    def test_apache_nodejs_default_port_rhel_passes_syntax_check(self):
        """Default port 3000, RHEL log paths — TCP proxy variant."""
        rendered = _render(
            "apache",
            "nodejs-proxy",
            domain="node-app.local",
            port=80,
            node_port=3000,
            node_socket=None,
            os_family="rhel_family",
        )
        passed, output = _httpd_test(rendered)
        assert (
            passed
        ), f"httpd -t FAILED.\nRendered config fragment:\n{rendered}\nhttpd output:\n{output}"

    @requires_httpd
    def test_apache_nodejs_custom_port_rhel_passes_syntax_check(self):
        """Custom port 8080, RHEL log paths."""
        rendered = _render(
            "apache",
            "nodejs-proxy",
            domain="node-app.local",
            port=80,
            node_port=8080,
            node_socket=None,
            os_family="rhel_family",
        )
        passed, output = _httpd_test(rendered)
        assert (
            passed
        ), f"httpd -t FAILED.\nRendered config fragment:\n{rendered}\nhttpd output:\n{output}"

    @requires_httpd
    def test_apache_nodejs_unix_socket_rhel_passes_syntax_check(self):
        """Unix Domain Socket proxy — `unix:<path>|http://localhost/` format."""
        rendered = _render(
            "apache",
            "nodejs-proxy",
            domain="node-app.local",
            port=80,
            node_port=3000,
            node_socket="/run/node-app/app.sock",
            os_family="rhel_family",
        )
        passed, output = _httpd_test(rendered)
        assert (
            passed
        ), f"httpd -t FAILED.\nRendered config fragment:\n{rendered}\nhttpd output:\n{output}"

    @requires_httpd
    def test_apache_nodejs_www_domain_redirect_passes_syntax_check(self):
        """www.* domain — canonical redirect direction reversed, RHEL paths."""
        rendered = _render(
            "apache",
            "nodejs-proxy",
            domain="www.node-app.local",
            port=80,
            node_port=3000,
            node_socket=None,
            os_family="rhel_family",
        )
        passed, output = _httpd_test(rendered)
        assert (
            passed
        ), f"httpd -t FAILED.\nRendered config fragment:\n{rendered}\nhttpd output:\n{output}"

    @requires_httpd
    def test_apache_nodejs_debian_log_paths_passes_syntax_check(self):
        """Debian/Ubuntu log path variant: ${APACHE_LOG_DIR} is substituted to /tmp."""
        rendered = _render(
            "apache",
            "nodejs-proxy",
            domain="node-app.local",
            port=80,
            node_port=3000,
            node_socket=None,
            os_family="debian_family",
        )
        # _httpd_test replaces ${APACHE_LOG_DIR}/ with a writable tmp path
        passed, output = _httpd_test(rendered)
        assert (
            passed
        ), f"httpd -t FAILED.\nRendered config fragment:\n{rendered}\nhttpd output:\n{output}"
