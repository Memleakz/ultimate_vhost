"""
Unit tests for ULTIMATE_VHOST-024: Interactive Directory Scaffolding.

Tests cover:
- Template rendering (pure function, no mocks)
- is_directory_empty() helper (filesystem only)
- CLI flag mutual exclusion guards
- Interactive prompts (TTY simulation via _is_tty mock + CliRunner input)
- Non-interactive flag-driven behaviour
"""

import pytest
from typer.testing import CliRunner
from vhost_helper.main import app
from vhost_helper.scaffolding import render_index_html, is_directory_empty
import vhost_helper.providers.nginx

runner = CliRunner()


# ---------------------------------------------------------------------------
# Pure function tests — no CLI, no mocks
# ---------------------------------------------------------------------------


def test_render_index_html_contains_it_works():
    html = render_index_html(domain="mysite.test")
    assert "It works!" in html


def test_render_index_html_contains_powered_by():
    html = render_index_html(domain="mysite.test")
    assert "Powered by ultimate_vhost" in html


def test_render_index_html_contains_domain():
    html = render_index_html(domain="example.test")
    assert "example.test" in html


def test_render_index_html_title_contains_domain():
    html = render_index_html(domain="titletest.test")
    assert "titletest.test" in html
    assert "<title>" in html


def test_render_index_html_valid_html5_structure():
    html = render_index_html(domain="mysite.test")
    assert "<!DOCTYPE html>" in html
    assert "<html" in html
    assert "<head>" in html
    assert "<body>" in html


def test_render_index_html_no_javascript():
    html = render_index_html(domain="mysite.test")
    assert "<script" not in html


def test_render_index_html_no_external_cdn():
    html = render_index_html(domain="mysite.test")
    assert "cdn." not in html.lower()
    assert "https://" not in html


def test_render_index_html_with_version():
    html = render_index_html(domain="mysite.test", tool_version="0.3.0")
    assert "ultimate_vhost 0.3.0" in html


def test_render_index_html_provider_shown():
    html = render_index_html(domain="mysite.test", provider="nginx")
    assert "Nginx" in html


def test_render_index_html_document_root_shown():
    html = render_index_html(domain="mysite.test", document_root="/var/www/mysite")
    assert "/var/www/mysite" in html


def test_render_index_html_custom_tool_name():
    html = render_index_html(domain="mysite.test", tool_name="myvhost")
    assert "Powered by myvhost" in html


def test_is_directory_empty_empty_dir(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert is_directory_empty(d) is True


def test_is_directory_empty_with_file(tmp_path):
    d = tmp_path / "nonempty"
    d.mkdir()
    (d / "index.html").write_text("<h1>hi</h1>")
    assert is_directory_empty(d) is False


def test_is_directory_empty_with_subdirectory(tmp_path):
    d = tmp_path / "hassubdir"
    d.mkdir()
    (d / "assets").mkdir()
    assert is_directory_empty(d) is False


def test_is_directory_empty_nonexistent_path(tmp_path):
    assert is_directory_empty(tmp_path / "ghost") is False


def test_is_directory_empty_hidden_file_counts(tmp_path):
    d = tmp_path / "hidden"
    d.mkdir()
    (d / ".htaccess").write_text("Options -Indexes")
    assert is_directory_empty(d) is False


# ---------------------------------------------------------------------------
# CLI fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nginx_env(mocker, tmp_path):
    """Sets up a minimal Nginx environment for scaffolding CLI tests."""
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
    mocker.patch("vhost_helper.providers.nginx.NginxProvider.remove_vhost")

    return tmp_path


# ---------------------------------------------------------------------------
# Flag mutual exclusion guards
# ---------------------------------------------------------------------------


def test_create_dir_and_no_create_dir_are_mutually_exclusive(mock_nginx_env):
    doc = mock_nginx_env / "site"
    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--create-dir", "--no-create-dir"],
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


def test_scaffold_and_no_scaffold_are_mutually_exclusive(mock_nginx_env):
    doc = mock_nginx_env / "site"
    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--scaffold", "--no-scaffold"],
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.stdout


# ---------------------------------------------------------------------------
# --no-create-dir behaviour
# ---------------------------------------------------------------------------


def test_no_create_dir_exits_1_when_directory_missing(mock_nginx_env, mocker):
    doc = mock_nginx_env / "nonexistent"
    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--no-create-dir"],
    )
    assert result.exit_code == 1
    assert "does not exist" in result.stdout


# ---------------------------------------------------------------------------
# --create-dir flag (non-interactive path)
# ---------------------------------------------------------------------------


def test_create_dir_flag_creates_directory_silently(mock_nginx_env, mocker):
    doc = mock_nginx_env / "newsite"

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    mock_create = mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--create-dir", "--no-scaffold"],
    )
    assert result.exit_code == 0
    mock_create.assert_called_once()
    assert doc.exists()


# ---------------------------------------------------------------------------
# --scaffold flag (generates index.html without prompting)
# ---------------------------------------------------------------------------


def test_create_dir_and_scaffold_flags_no_prompts(mock_nginx_env, mocker):
    """--create-dir --scaffold: directory created, index.html written, no prompts."""
    doc = mock_nginx_env / "newsite"

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--create-dir", "--scaffold"],
    )
    assert result.exit_code == 0
    mock_write.assert_called_once()

    kwargs = mock_write.call_args.kwargs
    args = mock_write.call_args.args
    content = kwargs.get("content") or (args[0] if args else None)
    assert content is not None
    assert "Powered by ultimate_vhost" in content
    assert "It works!" in content
    assert "mysite.test" in content


# ---------------------------------------------------------------------------
# Interactive TTY — simulated stdin via CliRunner input
# ---------------------------------------------------------------------------


def test_interactive_yes_to_dir_yes_to_scaffold(mock_nginx_env, mocker):
    """TTY: 'y\\ny\\n' → dir created and index.html generated."""
    doc = mock_nginx_env / "newsite"

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=True)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc)],
        input="y\ny\n",
    )
    assert result.exit_code == 0
    mock_write.assert_called_once()

    kwargs = mock_write.call_args.kwargs
    args = mock_write.call_args.args
    content = kwargs.get("content") or (args[0] if args else None)
    assert "Powered by ultimate_vhost" in content
    assert "mysite.test" in content


def test_interactive_yes_to_dir_no_to_scaffold(mock_nginx_env, mocker):
    """TTY: 'y\\nn\\n' → dir created, index.html NOT generated."""
    doc = mock_nginx_env / "newsite"

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=True)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc)],
        input="y\nn\n",
    )
    assert result.exit_code == 0
    mock_write.assert_not_called()


def test_interactive_no_to_dir_creation_graceful_abort(mock_nginx_env, mocker):
    """TTY: 'n\\n' → graceful abort with exit code 0, no files created."""
    doc = mock_nginx_env / "newsite"

    mock_create = mocker.patch("vhost_helper.main.create_directory_privileged")
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=True)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc)],
        input="n\n",
    )
    assert result.exit_code == 0
    mock_create.assert_not_called()
    mock_write.assert_not_called()
    assert "Aborting" in result.stdout


# ---------------------------------------------------------------------------
# Existing directory edge cases
# ---------------------------------------------------------------------------


def test_scaffold_not_triggered_for_nonempty_directory(mock_nginx_env, mocker):
    """Scaffolding prompt is suppressed when the directory already has content."""
    doc = mock_nginx_env / "existing"
    doc.mkdir()
    (doc / "app.py").write_text("print('hello')")

    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=True)

    result = runner.invoke(app, ["create", "mysite.test", str(doc)])
    assert result.exit_code == 0
    mock_write.assert_not_called()


def test_scaffold_triggered_for_empty_existing_directory(mock_nginx_env, mocker):
    """Scaffolding prompt IS shown when the directory exists but is empty."""
    doc = mock_nginx_env / "emptydir"
    doc.mkdir()

    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=True)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc)],
        input="y\n",
    )
    assert result.exit_code == 0
    mock_write.assert_called_once()


def test_no_scaffold_flag_suppresses_generation_for_empty_dir(mock_nginx_env, mocker):
    """--no-scaffold never generates index.html even in an empty directory."""
    doc = mock_nginx_env / "emptydir"
    doc.mkdir()

    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=True)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--no-scaffold"],
    )
    assert result.exit_code == 0
    mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# Non-TTY auto-create behaviour
# ---------------------------------------------------------------------------


def test_non_tty_no_flags_auto_creates_directory(mock_nginx_env, mocker):
    """Non-TTY without flags: directory auto-created, no scaffold (safe default)."""
    doc = mock_nginx_env / "auto"

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    mock_create = mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(app, ["create", "mysite.test", str(doc)])
    assert result.exit_code == 0
    mock_create.assert_called_once()
    # Non-TTY without --scaffold: no index.html generated
    mock_write.assert_not_called()


def test_non_tty_scaffold_flag_generates_html(mock_nginx_env, mocker):
    """Non-TTY with --scaffold: directory auto-created and index.html generated."""
    doc = mock_nginx_env / "auto"

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        ["create", "mysite.test", str(doc), "--scaffold"],
    )
    assert result.exit_code == 0
    mock_write.assert_called_once()
