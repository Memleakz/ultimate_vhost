"""
QA additions for ULTIMATE_VHOST-024: Interactive Directory Scaffolding.

Covers gaps not addressed by the implementation's own test suite:
  - write_index_html temporary-file cleanup on error
  - create_directory_privileged RuntimeError handling inside vhost create
  - _is_tty() basic behaviour
  - is_directory_empty on a file path (edge case)
  - Jinja2 autoescape prevents XSS in domain name
  - Template rendering with empty/None optional fields
  - --create-dir when directory already exists (idempotent, no double-mkdir)
  - Aborting message presence in graceful-abort flow
  - Scaffolding error logged as warning (not fatal)
  - index.html dest_path is absolute
  - Provider name capitalised in rendered HTML (nginx → Nginx)
  - Non-TTY auto-create prints creation confirmation
"""

import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from typer.testing import CliRunner

from vhost_helper.main import app
from vhost_helper.scaffolding import (
    _is_tty,
    create_directory_privileged,
    is_directory_empty,
    render_index_html,
    write_index_html,
)
import vhost_helper.providers.nginx

runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def nginx_env(mocker, tmp_path):
    """Minimal Nginx environment with all privileged/OS calls mocked."""
    available = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    available.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", available)

    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)
    mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")

    return tmp_path


# ---------------------------------------------------------------------------
# _is_tty() unit tests
# ---------------------------------------------------------------------------


def test_is_tty_returns_bool():
    result = _is_tty()
    assert isinstance(result, bool)


def test_is_tty_reflects_stdin_isatty(mocker):
    mocker.patch("sys.stdin")
    import sys
    sys.stdin.isatty.return_value = True
    assert _is_tty() is True

    sys.stdin.isatty.return_value = False
    assert _is_tty() is False


# ---------------------------------------------------------------------------
# is_directory_empty — extra edge cases
# ---------------------------------------------------------------------------


def test_is_directory_empty_on_file_path(tmp_path):
    """A file path (not a directory) must return False, not raise."""
    f = tmp_path / "notadir.txt"
    f.write_text("hello")
    assert is_directory_empty(f) is False


def test_is_directory_empty_on_symlink_to_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert is_directory_empty(link) is True


def test_is_directory_empty_with_only_dotfiles(tmp_path):
    d = tmp_path / "dot"
    d.mkdir()
    (d / ".hidden").write_text("x")
    assert is_directory_empty(d) is False


# ---------------------------------------------------------------------------
# render_index_html — edge / security cases
# ---------------------------------------------------------------------------


def test_render_index_html_xss_domain_is_escaped():
    """Domain containing HTML/JS must be escaped by Jinja2 autoescape."""
    evil = "<script>alert('xss')</script>"
    html = render_index_html(domain=evil)
    # The literal string must NOT appear; it must be escaped
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_index_html_empty_provider_omits_webserver_cell():
    html = render_index_html(domain="mysite.test", provider="")
    assert "Web Server" not in html


def test_render_index_html_empty_document_root_omits_docroot_cell():
    html = render_index_html(domain="mysite.test", document_root="")
    assert "Document Root" not in html


def test_render_index_html_none_tool_version_omits_version():
    html = render_index_html(domain="mysite.test", tool_version="")
    assert "ultimate_vhost" in html
    # No spurious trailing space or version string
    assert "ultimate_vhost " not in html


def test_render_index_html_provider_capitalised():
    """The template must capitalise the provider name (nginx → Nginx)."""
    html = render_index_html(domain="mysite.test", provider="nginx")
    assert "Nginx" in html
    # Should NOT render the raw lowercase string in the info cell
    # (the template uses {{ provider | capitalize }})


def test_render_index_html_apache_provider_capitalised():
    html = render_index_html(domain="mysite.test", provider="apache")
    assert "Apache" in html


# ---------------------------------------------------------------------------
# write_index_html — temp-file cleanup on error
# ---------------------------------------------------------------------------


def test_write_index_html_cleans_up_tempfile_on_error(mocker, tmp_path):
    """If sudo mv fails, the temp file must be deleted and RuntimeError re-raised."""
    captured_tmp_path: list[Path] = []

    def fake_run(cmd, **kwargs):
        if "mv" in cmd:
            # Capture the temp file path from the mv command
            captured_tmp_path.append(Path(cmd[-2]))
            raise RuntimeError("mv failed")
        return subprocess.CompletedProcess(cmd, 0)

    mocker.patch("vhost_helper.scaffolding.get_sudo_prefix", return_value=[])
    mocker.patch(
        "vhost_helper.scaffolding.run_elevated_command",
        side_effect=fake_run,
    )

    dest = tmp_path / "index.html"
    with pytest.raises(RuntimeError, match="mv failed"):
        write_index_html(
            content="<html></html>",
            dest_path=dest,
            user="www-data",
            group="www-data",
        )

    # The temp file must have been cleaned up
    if captured_tmp_path:
        assert not captured_tmp_path[0].exists(), "Temp file was not cleaned up"


def test_write_index_html_calls_chown_and_chmod(mocker, tmp_path):
    """write_index_html must call chown and chmod 644 after mv."""
    calls_seen: list[list[str]] = []

    def fake_run(cmd):
        calls_seen.append(list(cmd))
        # For mv: actually copy the file so dest_path exists for chown/chmod
        if "mv" in cmd:
            import shutil
            shutil.copy(cmd[-2], cmd[-1])

    mocker.patch("vhost_helper.scaffolding.get_sudo_prefix", return_value=[])
    mocker.patch(
        "vhost_helper.scaffolding.run_elevated_command",
        side_effect=fake_run,
    )

    dest = tmp_path / "index.html"
    write_index_html(
        content="<html>hi</html>",
        dest_path=dest,
        user="myuser",
        group="mygroup",
    )

    # Verify commands were issued
    cmds = [c[0] for c in calls_seen]
    assert "mv" in cmds
    assert "chown" in cmds
    assert "chmod" in cmds

    # chmod must set 644
    chmod_cmd = next(c for c in calls_seen if c[0] == "chmod")
    assert "644" in chmod_cmd


# ---------------------------------------------------------------------------
# create_directory_privileged — RuntimeError propagation in CLI
# ---------------------------------------------------------------------------


def test_create_dir_privileged_error_is_reported_to_user(nginx_env, mocker):
    """If create_directory_privileged raises, the CLI prints an error panel and exits 1."""
    doc = nginx_env / "newsite"

    mocker.patch(
        "vhost_helper.main.create_directory_privileged",
        side_effect=RuntimeError("Permission denied creating /var/www"),
    )
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--create-dir"],
    )
    assert result.exit_code == 1
    assert "Directory Creation Failed" in result.stdout or "Permission denied" in result.stdout


# ---------------------------------------------------------------------------
# --create-dir when directory already exists
# ---------------------------------------------------------------------------


def test_create_dir_flag_with_existing_directory_does_not_create(nginx_env, mocker):
    """--create-dir when dir already exists must NOT call create_directory_privileged."""
    doc = nginx_env / "existing"
    doc.mkdir()

    mock_create = mocker.patch("vhost_helper.main.create_directory_privileged")
    mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--create-dir", "--no-scaffold"],
    )
    assert result.exit_code == 0
    mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# Graceful abort message
# ---------------------------------------------------------------------------


def test_graceful_abort_contains_aborting_keyword(nginx_env, mocker):
    """User declining dir creation must produce an 'Aborting' message and exit 0."""
    doc = nginx_env / "ghostdir"
    mocker.patch("vhost_helper.main._is_tty", return_value=True)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc)],
        input="n\n",
    )
    assert result.exit_code == 0
    assert "Aborting" in result.stdout


# ---------------------------------------------------------------------------
# Scaffolding failure is non-fatal (warning only)
# ---------------------------------------------------------------------------


def test_scaffold_write_failure_logged_as_warning_not_fatal(nginx_env, mocker):
    """If write_index_html raises, the command must still succeed (exit 0)."""
    doc = nginx_env / "emptydir"
    doc.mkdir()

    mocker.patch(
        "vhost_helper.main.write_index_html",
        side_effect=RuntimeError("disk full"),
    )
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--scaffold"],
    )
    assert result.exit_code == 0
    # Warning must be printed to stdout
    assert "Could not generate index.html" in result.stdout or "disk full" in result.stdout


# ---------------------------------------------------------------------------
# index.html dest_path is absolute
# ---------------------------------------------------------------------------


def test_index_html_dest_path_is_absolute(nginx_env, mocker):
    """The dest_path passed to write_index_html must be an absolute Path."""
    doc = nginx_env / "sitedir"
    doc.mkdir()

    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--scaffold"],
    )

    mock_write.assert_called_once()
    kwargs = mock_write.call_args.kwargs
    args = mock_write.call_args.args
    dest = kwargs.get("dest_path") or (args[1] if len(args) > 1 else None)
    assert dest is not None
    assert dest.is_absolute()


# ---------------------------------------------------------------------------
# Non-TTY auto-create prints confirmation message
# ---------------------------------------------------------------------------


def test_non_tty_auto_create_prints_confirmation(nginx_env, mocker):
    """Non-TTY run must print a confirmation when it auto-creates the directory."""
    doc = nginx_env / "auto"

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(app, ["create", "mysite.test", str(doc)])
    assert result.exit_code == 0
    # Should print a green confirmation that the directory was created
    assert "Directory" in result.stdout and "created" in result.stdout


# ---------------------------------------------------------------------------
# Template: "It works!" text appears exactly once in rendered HTML
# ---------------------------------------------------------------------------


def test_render_index_html_it_works_appears_once():
    html = render_index_html(domain="mysite.test")
    count = html.count("It works!")
    assert count == 1, f"Expected exactly 1 occurrence of 'It works!', got {count}"


# ---------------------------------------------------------------------------
# Template: DOCTYPE html is case-correct
# ---------------------------------------------------------------------------


def test_render_index_html_doctype_is_html5():
    html = render_index_html(domain="mysite.test")
    assert "<!DOCTYPE html>" in html


# ---------------------------------------------------------------------------
# Template: no CDN or https:// links (self-contained)
# ---------------------------------------------------------------------------


def test_render_index_html_no_http_links():
    html = render_index_html(domain="mysite.test")
    assert "http://" not in html
    assert "https://" not in html
