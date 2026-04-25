"""
Integration tests for ULTIMATE_VHOST-024: end-to-end create → directory → index.html.

These tests verify the full `vhost create` flow with filesystem mocks for both
Nginx and Apache providers, confirming provider-agnostic scaffolding behaviour.
"""

import pytest
from typer.testing import CliRunner
from pathlib import Path
from vhost_helper.main import app
import vhost_helper.providers.nginx
import vhost_helper.providers.apache

runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _extract_html_content(mock_write):
    """Return the rendered HTML string from a write_index_html mock call."""
    kwargs = mock_write.call_args.kwargs
    args = mock_write.call_args.args
    return kwargs.get("content") or (args[0] if args else None)


def _extract_dest_path(mock_write):
    """Return the dest_path argument from a write_index_html mock call."""
    kwargs = mock_write.call_args.kwargs
    args = mock_write.call_args.args
    return kwargs.get("dest_path") or (args[1] if len(args) > 1 else None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def nginx_env(mocker, tmp_path):
    """Full Nginx provider environment with all external calls mocked."""
    available = tmp_path / "nginx-available"
    enabled = tmp_path / "nginx-enabled"
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


@pytest.fixture
def apache_env(mocker, tmp_path):
    """Full Apache provider environment with all external calls mocked."""
    available = tmp_path / "apache-available"
    enabled = tmp_path / "apache-enabled"
    available.mkdir()
    enabled.mkdir()

    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_AVAILABLE", available)
    mocker.patch("vhost_helper.providers.apache.APACHE_SITES_ENABLED", enabled)

    mocker.patch("vhost_helper.main.is_nginx_installed", return_value=False)
    mocker.patch("vhost_helper.main.is_apache_installed", return_value=True)
    mocker.patch("vhost_helper.main.is_apache_running", return_value=False)

    mocker.patch("vhost_helper.providers.apache.ApacheProvider.create_vhost")
    mocker.patch("vhost_helper.providers.apache.ApacheProvider.remove_vhost")

    return tmp_path


# ---------------------------------------------------------------------------
# Nginx integration tests
# ---------------------------------------------------------------------------


def test_nginx_create_dir_and_scaffold_nonexistent_path(nginx_env, mocker):
    """
    vhost create app.test /nonexistent --provider nginx --create-dir --scaffold
    Expected: directory created, vhost config invoked, index.html written.
    """
    doc = nginx_env / "app"

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        [
            "create",
            "app.test",
            str(doc),
            "--provider", "nginx",
            "--create-dir",
            "--scaffold",
        ],
    )
    assert result.exit_code == 0, result.stdout

    # Directory was created
    assert doc.exists()

    # Provider was invoked
    vhost_helper.providers.nginx.NginxProvider.create_vhost.assert_called_once()

    # index.html was written
    mock_write.assert_called_once()
    content = _extract_html_content(mock_write)
    assert content is not None
    assert "Powered by ultimate_vhost" in content
    assert "It works!" in content
    assert "app.test" in content


def test_nginx_create_dir_with_no_scaffold(nginx_env, mocker):
    """
    vhost create app.test /nonexistent --provider nginx --create-dir --no-scaffold
    Expected: directory created, vhost config invoked, index.html NOT written.
    """
    doc = nginx_env / "app"

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        [
            "create",
            "app.test",
            str(doc),
            "--provider", "nginx",
            "--create-dir",
            "--no-scaffold",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert doc.exists()
    mock_write.assert_not_called()


def test_nginx_existing_empty_dir_scaffold(nginx_env, mocker):
    """
    --create-dir --scaffold where directory already exists and is empty.
    Expected: no mkdir called, index.html written.
    """
    doc = nginx_env / "existing-empty"
    doc.mkdir()

    mock_create = mocker.patch("vhost_helper.main.create_directory_privileged")
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        [
            "create",
            "app.test",
            str(doc),
            "--provider", "nginx",
            "--scaffold",
        ],
    )
    assert result.exit_code == 0, result.stdout
    # No directory creation command invoked
    mock_create.assert_not_called()
    # index.html was generated
    mock_write.assert_called_once()


def test_nginx_existing_nonempty_dir_no_scaffold(nginx_env, mocker):
    """
    Non-empty existing directory: scaffolding is never triggered.
    """
    doc = nginx_env / "existing-nonempty"
    doc.mkdir()
    (doc / "index.html").write_text("<h1>Already here</h1>")

    mock_create = mocker.patch("vhost_helper.main.create_directory_privileged")
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=True)

    result = runner.invoke(
        app,
        ["create", "app.test", str(doc), "--provider", "nginx"],
    )
    assert result.exit_code == 0, result.stdout
    mock_create.assert_not_called()
    mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# Apache integration tests (provider-agnostic parity)
# ---------------------------------------------------------------------------


def test_apache_create_dir_and_scaffold(apache_env, mocker):
    """
    vhost create app.test /nonexistent --provider apache --create-dir --scaffold
    Expected: directory created, apache config invoked, index.html written.
    """
    doc = apache_env / "app"

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        [
            "create",
            "app.test",
            str(doc),
            "--provider", "apache",
            "--create-dir",
            "--scaffold",
        ],
    )
    assert result.exit_code == 0, result.stdout

    assert doc.exists()
    vhost_helper.providers.apache.ApacheProvider.create_vhost.assert_called_once()

    mock_write.assert_called_once()
    content = _extract_html_content(mock_write)
    assert "Powered by ultimate_vhost" in content
    assert "It works!" in content
    assert "app.test" in content


def test_apache_existing_empty_dir_scaffold(apache_env, mocker):
    """
    Apache provider: empty existing directory triggers scaffold with --scaffold flag.
    """
    doc = apache_env / "empty"
    doc.mkdir()

    mock_create = mocker.patch("vhost_helper.main.create_directory_privileged")
    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result = runner.invoke(
        app,
        ["create", "app.test", str(doc), "--provider", "apache", "--scaffold"],
    )
    assert result.exit_code == 0, result.stdout
    mock_create.assert_not_called()
    mock_write.assert_called_once()


# ---------------------------------------------------------------------------
# dest_path validation
# ---------------------------------------------------------------------------


def test_index_html_written_to_correct_path(nginx_env, mocker):
    """index.html dest_path must be <document_root>/index.html."""
    doc = nginx_env / "sitedir"
    doc.mkdir()

    mock_write = mocker.patch("vhost_helper.main.write_index_html")
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    runner.invoke(
        app,
        ["create", "app.test", str(doc), "--provider", "nginx", "--scaffold"],
    )

    dest = _extract_dest_path(mock_write)
    assert dest is not None
    assert dest == doc.absolute() / "index.html"


# ---------------------------------------------------------------------------
# Non-TTY parity with interactive Y/Y flow
# ---------------------------------------------------------------------------


def test_non_tty_flags_match_interactive_yy_output(nginx_env, mocker):
    """
    --create-dir --scaffold in non-TTY produces the same file output as
    interactive 'y\\ny\\n' in a TTY.
    """
    doc_flag = nginx_env / "flag-site"
    doc_tty = nginx_env / "tty-site"

    html_from_flags = None
    html_from_tty = None

    def capture_flag(content, dest_path, user, group):
        nonlocal html_from_flags
        html_from_flags = content

    def capture_tty(content, dest_path, user, group):
        nonlocal html_from_tty
        html_from_tty = content

    def _make_dir(path, user, group):
        path.mkdir(parents=True, exist_ok=True)

    # ---- Non-TTY run ----
    mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mocker.patch(
        "vhost_helper.main.write_index_html", side_effect=capture_flag
    )
    mocker.patch("vhost_helper.main._is_tty", return_value=False)

    result1 = runner.invoke(
        app,
        [
            "create",
            "app.test",
            str(doc_flag),
            "--create-dir",
            "--scaffold",
        ],
    )
    assert result1.exit_code == 0

    # ---- TTY run ----
    mocker.patch(
        "vhost_helper.main.create_directory_privileged", side_effect=_make_dir
    )
    mocker.patch(
        "vhost_helper.main.write_index_html", side_effect=capture_tty
    )
    mocker.patch("vhost_helper.main._is_tty", return_value=True)

    result2 = runner.invoke(
        app,
        ["create", "app.test", str(doc_tty)],
        input="y\ny\n",
    )
    assert result2.exit_code == 0

    # Both should contain the same domain-invariant content
    assert html_from_flags is not None
    assert html_from_tty is not None
    # The rendered HTML is structurally identical except for the document_root
    # path that is embedded in the info grid; assert all key invariants match.
    for expected in ("It works!", "Powered by ultimate_vhost", "app.test", "<!DOCTYPE html>"):
        assert expected in html_from_flags, f"flags output missing: {expected}"
        assert expected in html_from_tty, f"tty output missing: {expected}"
