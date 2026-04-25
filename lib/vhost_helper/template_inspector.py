"""
Template Variable Discovery and Inspection Utility.

Provides dynamic, zero-registry introspection of Jinja2 templates in
src/templates/. The CLI uses this module to power:
  - `vhost templates list [--provider <name>]`
  - `vhost templates inspect <provider>-<mode>`

Adding a new .conf.j2 file to src/templates/<provider>/ makes it
automatically discoverable — no code changes required.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

try:
    import yaml as _yaml

    _YAML_AVAILABLE = True
except ImportError:  # pyyaml is an optional dependency
    _yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False

from jinja2 import Environment
from jinja2 import meta as jinja2_meta

# Delimiter pattern for the YAML metadata block embedded in a Jinja2 comment.
# Matches: {# ---\n<yaml content>\n--- #}
_METADATA_RE = re.compile(r"\{#\s*---\n(.*?)\n\s*---\s*#\}", re.DOTALL)

# Jinja2 loop-scoped and special built-in names that are not template inputs.
_JINJA2_BUILTINS: frozenset[str] = frozenset({"loop", "super", "varargs", "kwargs"})


def extract_variables(template_path: Path) -> list[str]:
    """Return all undeclared input variable names from a Jinja2 template.

    Uses ``jinja2.Environment.parse()`` and
    ``jinja2.meta.find_undeclared_variables()`` for static AST analysis —
    the template is never rendered. Jinja2 built-in names (``loop``,
    ``super``, ``varargs``, ``kwargs``) are excluded from results.

    Never raises an exception: returns an empty list on any parse error,
    making the function safe to call in parametrized test suites over
    arbitrary template files.

    Args:
        template_path: Absolute or relative path to the ``.j2`` template file.

    Returns:
        Sorted list of unique undeclared variable name strings.
    """
    try:
        source = template_path.read_text(encoding="utf-8")
        env = Environment(autoescape=True)
        ast = env.parse(source)
        variables = jinja2_meta.find_undeclared_variables(ast)
        return sorted(variables - _JINJA2_BUILTINS)
    except Exception:  # noqa: BLE001 — graceful degradation is a documented contract
        return []


def extract_metadata(template_path: Path) -> dict[str, dict]:
    """Parse the structured YAML variable description block from a ``.j2`` template.

    Expects a leading Jinja2 comment block in the following format::

        {# ---
        variables:
          - name: domain
            description: "The fully-qualified domain name for the virtual host."
          - name: port
            description: "HTTP port the server block listens on."
            default: "80"
        --- #}

    Returns an empty dict (without raising) when the block is absent or
    malformed — graceful degradation is a hard requirement so that
    uninstrumented templates do not break the CLI.

    Args:
        template_path: Absolute or relative path to the ``.j2`` template file.

    Returns:
        Dict mapping variable name to ``{"description": str, "default": str | None}``.
        Returns ``{}`` if no valid metadata block is found.
    """
    try:
        source = template_path.read_text(encoding="utf-8")
        match = _METADATA_RE.search(source)
        if not match:
            return {}

        if not _YAML_AVAILABLE:
            return {}

        data = _yaml.safe_load(match.group(1))
        if not isinstance(data, dict) or "variables" not in data:
            return {}

        result: dict[str, dict] = {}
        for entry in data.get("variables", []):
            if isinstance(entry, dict) and "name" in entry:
                result[entry["name"]] = {
                    "description": entry.get("description", "No description provided"),
                    "default": entry.get("default"),
                }
        return result
    except Exception:  # noqa: BLE001 — graceful degradation
        return {}


def _is_safe_path_component(name: str) -> bool:
    """Return True if *name* is a safe single path component.

    A safe component contains only alphanumerics, hyphens, and underscores, has
    no path separators, and is not the special relative reference ``..``.  This
    prevents path-traversal attacks when user-supplied names are appended to
    base directory paths.
    """
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    import re as _re

    return bool(_re.fullmatch(r"[A-Za-z0-9_\-]+", name))


def list_templates(
    templates_dir: Path,
    provider: Optional[str] = None,
) -> dict[str, list[str]]:
    """Scan the templates directory and return available template names.

    Zero-registry design: new ``.conf.j2`` files dropped into
    ``src/templates/<provider>/`` are auto-discovered without any code changes.
    The scan is anchored to the resolved absolute path of ``templates_dir`` to
    prevent path traversal issues when the tool is installed system-wide.

    Args:
        templates_dir: Absolute path to the ``src/templates/`` directory.
        provider:      If given, limits results to that provider subdirectory
                       only. Passing an unknown provider name returns ``{}``.

    Returns:
        Dict mapping provider name to a sorted list of mode names, where each
        mode name is the filename stem stripped of the ``.conf.j2`` suffix.

        Example::

            {"nginx": ["php", "python-proxy", "static"],
             "apache": ["php-fpm", "python-proxy", "static"]}
    """
    templates_dir = templates_dir.resolve()
    result: dict[str, list[str]] = {}

    if not templates_dir.is_dir():
        return result

    # Validate the provider name before using it as a path component.
    if provider is not None and not _is_safe_path_component(provider):
        return result

    provider_dirs: list[Path]
    if provider is not None:
        provider_dirs = [templates_dir / provider]
    else:
        provider_dirs = sorted(p for p in templates_dir.iterdir() if p.is_dir())

    for provider_dir in provider_dirs:
        if not provider_dir.is_dir():
            continue
        modes = sorted(
            f.name[: -len(".conf.j2")]
            for f in provider_dir.glob("*.conf.j2")
            if f.name.endswith(".conf.j2")
        )
        if modes:
            result[provider_dir.name] = modes

    return result


def resolve_template_path(name: str, templates_dir: Path) -> Optional[Path]:
    """Resolve a ``<provider>-<mode>`` template name to its filesystem path.

    Convention: the first hyphen-delimited segment is the provider; the
    remainder forms the mode name, supporting multi-word modes such as
    ``php-fpm`` and ``python-proxy``.

    Examples::

        "nginx-php"          → templates_dir / "nginx"  / "php.conf.j2"
        "apache-php-fpm"     → templates_dir / "apache" / "php-fpm.conf.j2"
        "apache-python-proxy"→ templates_dir / "apache" / "python-proxy.conf.j2"

    Args:
        name:          Template identifier in ``<provider>-<mode>`` format.
        templates_dir: Absolute path to the ``src/templates/`` directory.

    Returns:
        Resolved :class:`~pathlib.Path` if the file exists, otherwise ``None``.
    """
    parts = name.split("-", 1)
    if len(parts) != 2:
        return None

    provider, mode = parts

    # Validate both components to prevent path-traversal attacks.
    # provider must be a single safe identifier; mode may contain hyphens but
    # no directory separators or dot-dot sequences.
    if not _is_safe_path_component(provider):
        return None
    # Mode can contain hyphens between segments (e.g. "php-fpm"), but each
    # hyphen-separated segment must itself be safe.
    if not all(_is_safe_path_component(seg) for seg in mode.split("-")):
        return None

    base = templates_dir.resolve()
    candidate = (base / provider / f"{mode}.conf.j2").resolve()

    # Ensure the resolved candidate path is still inside templates_dir (confinement check).
    try:
        candidate.relative_to(base)
    except ValueError:
        return None

    return candidate if candidate.is_file() else None
