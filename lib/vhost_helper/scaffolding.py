"""
Scaffolding helpers for interactive document-root creation and index.html generation.

These functions are provider-agnostic and are called from the `vhost create` command
when the target document root is absent or empty.
"""

import os
import sys
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .config import APP_TEMPLATES_DIR
from .utils import get_sudo_prefix, run_elevated_command


def _is_tty() -> bool:
    """Returns True when stdout is connected to an interactive terminal."""
    return sys.stdin.isatty()


def create_directory_privileged(path: Path, user: str, group: str) -> None:
    """Creates a directory (and any missing parents) with correct ownership.

    Uses ``sudo mkdir -p``, ``sudo chown``, and ``sudo chmod`` via the existing
    privileged-command wrapper so that the process never needs to be root itself.

    Args:
        path:  Absolute path of the directory to create.
        user:  Owner username (e.g. ``"www-data"`` or the current login user).
        group: Owner group  (e.g. ``"www-data"``).

    Raises:
        RuntimeError: If any privileged command returns a non-zero exit code.
    """
    sudo = get_sudo_prefix()
    path_str = str(path)

    run_elevated_command(sudo + ["mkdir", "-p", path_str])
    run_elevated_command(sudo + ["chown", f"{user}:{group}", path_str])
    run_elevated_command(sudo + ["chmod", "755", path_str])


def is_directory_empty(path: Path) -> bool:
    """Returns True if *path* exists, is a directory, and contains no entries.

    A directory is considered non-empty as soon as it has at least one child
    (file, symlink, or subdirectory).  Hidden files (dotfiles) count.
    """
    if not path.exists() or not path.is_dir():
        return False
    return not any(path.iterdir())


def render_index_html(
    domain: str,
    provider: str = "",
    document_root: str = "",
    tool_name: str = "ultimate_vhost",
    tool_version: str = "",
) -> str:
    """Renders the ``common/index.html.j2`` template.

    Args:
        domain:        Domain name shown as the page title and heading.
        provider:      Web server name shown in the info grid (e.g. ``"nginx"``).
        document_root: Absolute webroot path shown in the info grid.
        tool_name:     Product name for the "Powered by" footer line.
        tool_version:  Optional version string appended to ``tool_name``.

    Returns:
        Rendered HTML string.

    Raises:
        FileNotFoundError: If the template file cannot be located.
        jinja2.TemplateNotFound: If Jinja2 cannot load the template.
    """
    common_templates_dir = APP_TEMPLATES_DIR / "common"
    env = Environment(
        loader=FileSystemLoader(str(common_templates_dir)),
        autoescape=True,
    )
    template = env.get_template("index.html.j2")
    return template.render(
        domain=domain,
        provider=provider,
        document_root=document_root,
        tool_name=tool_name,
        tool_version=tool_version,
    )


def write_index_html(
    content: str,
    dest_path: Path,
    user: str,
    group: str,
) -> None:
    """Writes *content* to *dest_path* using a privileged ``mv`` + ``chown`` workflow.

    The content is first written to a temporary file (owned by the current user),
    then moved into place with ``sudo mv`` so that the tool never performs a
    direct ``open()`` on a path that may require elevated permissions.

    After the move, ``sudo chown`` and ``sudo chmod 644`` ensure the file has
    the correct ownership and permissions regardless of the ``umask`` in effect.

    Args:
        content:   UTF-8 string to write (the rendered HTML).
        dest_path: Absolute destination path (e.g. ``/var/www/app/index.html``).
        user:      File owner username.
        group:     File owner group.

    Raises:
        RuntimeError: If any privileged command fails.
    """
    sudo = get_sudo_prefix()

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False, suffix=".html"
    ) as tmp_file:
        tmp_file.write(content)
        tmp_file_path = Path(tmp_file.name)

    try:
        run_elevated_command(sudo + ["mv", str(tmp_file_path), str(dest_path)])
        run_elevated_command(sudo + ["chown", f"{user}:{group}", str(dest_path)])
        run_elevated_command(sudo + ["chmod", "644", str(dest_path)])
    except Exception:
        if tmp_file_path.exists():
            try:
                os.unlink(str(tmp_file_path))
            except OSError:
                pass
        raise
