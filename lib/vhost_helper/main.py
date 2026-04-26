import typer
import re
import os
import sys as _sys
import shutil
import subprocess
from contextlib import contextmanager
from typing import Optional, Union, List  # Added List
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from .config import (
    NGINX_SITES_AVAILABLE,
    NGINX_SITES_ENABLED,
    NGINX_SITES_DISABLED,
    APACHE_SITES_AVAILABLE,
    APACHE_SITES_ENABLED,
    APACHE_SITES_DISABLED,
    APP_TEMPLATES_DIR,
    initialize_user_config,
)
from .models import (
    VHostConfig,
    ServerType,
    RuntimeMode,
    PHP_SOCKET_PATHS,
    DEFAULT_PHP_SOCKET,
    VHostInfo,  # Added VHostInfo
)
from .hostfile import add_entry, remove_entry
from .providers.nginx import (
    NginxProvider,
    is_nginx_installed,
    is_nginx_running,
    _extract_nginx_vhost_details,
)
from .providers.apache import (
    ApacheProvider,
    is_apache_installed,
    is_apache_running,
    _extract_apache_vhost_details,
)
from .os_detector import get_os_info
from .utils import set_active_live, preflight_sudo_check
from .permissions import (
    resolve_webserver_user_group,
    get_current_user,
    validate_webroot_perms,
    validate_unix_name,
    apply_webroot_permissions,
    is_selinux_active,
    apply_selinux_webroot_context,
)
from .ssl import check_mkcert_binary, generate_certificate, get_ssl_dir
from .php_fpm import (
    PhpFpmNotFoundError,
    detect_default_version,
    validate_version_present,
    start_service,
    resolve_socket_path,
)
from .template_inspector import (
    list_templates,
    resolve_template_path,
    extract_variables,
    extract_metadata,
)
from .logs import extract_nginx_log_paths, extract_apache_log_paths
from .scaffolding import (
    _is_tty,
    create_directory_privileged,
    is_directory_empty,
    render_index_html,
    write_index_html,
)

app = typer.Typer(help="VHost Helper: Unified virtual host management.")
templates_app = typer.Typer(help="Inspect and list available Jinja2 templates.")
app.add_typer(templates_app, name="templates")
# When stdout is not a real TTY (e.g. captured with $(...) in shell scripts or
# piped through grep in CI), Rich falls back to 80-column width which truncates
# table rows.  We detect that case and use the COLUMNS env var, or a generous
# 220-column default, so list/info output is never silently cut off.
_console_width: Optional[int] = None
if not _sys.stdout.isatty():
    try:
        _console_width = int(os.environ.get("COLUMNS", "220"))
    except (ValueError, TypeError):
        _console_width = 220

console = Console(width=_console_width)

# Sentinel injected into argv when `--php` is used without a version argument.
# The real CLI preprocesses sys.argv so `--php` alone becomes `--php __auto__`.
_PHP_AUTO = "__auto__"

# Initialize user configuration directories on startup
initialize_user_config()


@contextmanager
def _tracked_status(*args, **kwargs):
    """Wraps console.status() and registers the live context for spinner suspension."""
    with console.status(*args, **kwargs) as status:
        set_active_live(status)
        try:
            yield status
        finally:
            set_active_live(None)


# Per-label validation: each DNS label must start and end with an alphanumeric
# character; hyphens are only permitted in the interior of a label.
DOMAIN_REGEX = r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$"


def validate_domain(domain: str) -> str:
    """Validates the domain name format to prevent injection, path traversal and invalid characters."""
    if not domain or len(domain) > 253:
        raise ValueError(f"Domain name too long or empty: '{domain}'")
    if ".." in domain:
        raise ValueError(f"Domain name cannot contain double dots: '{domain}'")
    if any(len(label) > 63 for label in domain.split(".")):
        raise ValueError(
            f"Domain label exceeds 63-character limit (RFC 1035): '{domain}'"
        )
    if not re.match(DOMAIN_REGEX, domain):
        raise ValueError(
            f"Invalid domain format: '{domain}'. Use only alphanumeric characters, dots, and hyphens. (Must be at least 3 characters)"
        )
    return domain


def get_redirect_domain(domain: str) -> str:
    """Returns the counterpart domain (www. variant or base variant)."""
    if domain.startswith("www."):
        return domain[4:]
    return f"www.{domain}"


def _get_provider(server_type: ServerType) -> Union[NginxProvider, ApacheProvider]:
    """Returns the appropriate provider instance."""
    if server_type == ServerType.NGINX:
        return NginxProvider()
    return ApacheProvider()


def _detect_provider_for_domain(domain: str) -> Optional[ServerType]:
    """Detects provider for an existing domain by checking config paths."""
    # Check Nginx
    nginx_potential = False
    if NGINX_SITES_AVAILABLE.exists():
        nginx_potential = True
        if (NGINX_SITES_AVAILABLE / (domain + ".conf")).exists():
            return ServerType.NGINX
    if NGINX_SITES_ENABLED.exists():
        nginx_potential = True
        if (NGINX_SITES_ENABLED / (domain + ".conf")).exists():
            return ServerType.NGINX
    if NGINX_SITES_DISABLED and NGINX_SITES_DISABLED.exists():
        nginx_potential = True
        if (NGINX_SITES_DISABLED / (domain + ".conf")).exists():
            return ServerType.NGINX

    # Check Apache
    apache_potential = False
    if APACHE_SITES_AVAILABLE.exists():
        apache_potential = True
        if (APACHE_SITES_AVAILABLE / (domain + ".conf")).exists():
            return ServerType.APACHE
    if APACHE_SITES_ENABLED.exists():
        apache_potential = True
        if (APACHE_SITES_ENABLED / (domain + ".conf")).exists():
            return ServerType.APACHE
    if APACHE_SITES_DISABLED and APACHE_SITES_DISABLED.exists():
        apache_potential = True
        if (APACHE_SITES_DISABLED / (domain + ".conf")).exists():
            return ServerType.APACHE

    # Fallback: if only one provider's directories exist, assume it
    if nginx_potential and not apache_potential:
        return ServerType.NGINX
    if apache_potential and not nginx_potential:
        return ServerType.APACHE

    return None


def _detect_server_type() -> ServerType:
    """Detects which web server is installed, favoring Nginx if both are present."""
    if is_nginx_installed():
        return ServerType.NGINX
    if is_apache_installed():
        return ServerType.APACHE
    raise RuntimeError("No supported web server found (Nginx or Apache).")


_MANAGED_HEADER = "# Generated by VHost Helper"


def _scan_all_vhosts_locally(
    filter_provider: Optional[ServerType] = None,
) -> List[VHostInfo]:
    """
    Scans all vhost configuration directories using this module's constants.
    These constants are patchable in tests, unlike the provider-level imports.
    Returns a deduplicated list of VHostInfo objects found across all dirs.
    """
    vhosts_by_domain: dict = {}

    # ------------------------------------------------------------------ Nginx
    if filter_provider is None or filter_provider == ServerType.NGINX:
        nginx_dirs = [
            d for d in [NGINX_SITES_AVAILABLE, NGINX_SITES_ENABLED] if d is not None
        ]
        if NGINX_SITES_DISABLED:
            nginx_dirs.append(NGINX_SITES_DISABLED)

        # Determine which domain stems are currently enabled (by filename in enabled dir)
        nginx_enabled_names: set = set()
        if NGINX_SITES_ENABLED and NGINX_SITES_ENABLED.exists():
            try:
                for p in NGINX_SITES_ENABLED.iterdir():
                    if p.name.endswith(".conf"):
                        nginx_enabled_names.add(p.stem)
            except (PermissionError, OSError):
                pass

        for directory in nginx_dirs:
            if not directory or not directory.exists():
                continue
            try:
                entries = list(directory.iterdir())
            except (PermissionError, OSError):
                continue
            for config_path in entries:
                if not config_path.name.endswith(".conf"):
                    continue
                try:
                    content = config_path.read_text()
                except PermissionError as exc:
                    console.print(
                        f"[yellow]Warning: Permission denied reading configuration "
                        f"{config_path.name}: {exc}[/yellow]"
                    )
                    continue
                except Exception as exc:
                    console.print(
                        f"[yellow]Warning: Error reading configuration "
                        f"{config_path.name}: {exc}[/yellow]"
                    )
                    continue

                try:
                    real_path = config_path.resolve()
                except (OSError, FileNotFoundError):
                    real_path = config_path

                domain_raw, doc_root = _extract_nginx_vhost_details(content, real_path)
                if not domain_raw:
                    continue
                domain_raw = domain_raw.strip("\"'")
                try:
                    domain_val = validate_domain(domain_raw)
                except ValueError:
                    continue

                if domain_val in vhosts_by_domain:
                    continue

                is_managed = _MANAGED_HEADER in content
                status = "Enabled" if domain_val in nginx_enabled_names else "Disabled"
                vhosts_by_domain[domain_val] = VHostInfo(
                    domain=domain_val,
                    config_path=real_path,
                    server_type=ServerType.NGINX,
                    status=status,
                    managed_by="VHost Helper" if is_managed else "External",
                    document_root=doc_root,
                )

    # ----------------------------------------------------------------- Apache
    if filter_provider is None or filter_provider == ServerType.APACHE:
        apache_dirs = [
            d for d in [APACHE_SITES_AVAILABLE, APACHE_SITES_ENABLED] if d is not None
        ]
        if APACHE_SITES_DISABLED:
            apache_dirs.append(APACHE_SITES_DISABLED)

        apache_enabled_names: set = set()
        if APACHE_SITES_ENABLED and APACHE_SITES_ENABLED.exists():
            try:
                for p in APACHE_SITES_ENABLED.iterdir():
                    if p.name.endswith(".conf"):
                        apache_enabled_names.add(p.stem)
            except (PermissionError, OSError):
                pass

        for directory in apache_dirs:
            if not directory or not directory.exists():
                continue
            try:
                entries = list(directory.iterdir())
            except (PermissionError, OSError):
                continue
            for config_path in entries:
                if not config_path.name.endswith(".conf"):
                    continue
                try:
                    content = config_path.read_text()
                except PermissionError as exc:
                    console.print(
                        f"[yellow]Warning: Permission denied reading configuration "
                        f"{config_path.name}: {exc}[/yellow]"
                    )
                    continue
                except Exception as exc:
                    console.print(
                        f"[yellow]Warning: Error reading configuration "
                        f"{config_path.name}: {exc}[/yellow]"
                    )
                    continue

                try:
                    real_path = config_path.resolve()
                except (OSError, FileNotFoundError):
                    real_path = config_path

                domain_raw, doc_root = _extract_apache_vhost_details(content, real_path)
                if not domain_raw:
                    continue
                domain_raw = domain_raw.strip("\"'")
                try:
                    domain_val = validate_domain(domain_raw)
                except ValueError:
                    continue

                if domain_val in vhosts_by_domain:
                    continue

                is_managed = _MANAGED_HEADER in content
                status = "Enabled" if domain_val in apache_enabled_names else "Disabled"
                vhosts_by_domain[domain_val] = VHostInfo(
                    domain=domain_val,
                    config_path=real_path,
                    server_type=ServerType.APACHE,
                    status=status,
                    managed_by="VHost Helper" if is_managed else "External",
                    document_root=doc_root,
                )

    return list(vhosts_by_domain.values())


def _find_vhost_info_for_domain(
    domain: str, provider: Optional[ServerType] = None
) -> Optional[VHostInfo]:
    """Finds a VHostInfo for a given domain by scanning this module's dir constants."""
    for vhost_info in _scan_all_vhosts_locally(filter_provider=provider):
        if vhost_info.domain == domain:
            return vhost_info
    return None


@app.command()
def create(
    domain: str = typer.Argument(..., help="The domain name (e.g., mysite.test)"),
    document_root: Path = typer.Argument(
        ..., help="The absolute path to the project root"
    ),
    provider: Optional[ServerType] = typer.Option(
        None, "--provider", "-p", help="Web server provider (nginx or apache)"
    ),
    port: int = typer.Option(80, help="Port number"),
    php: Optional[str] = typer.Option(
        None,
        "--php",
        help=(
            "Enable PHP-FPM support. Optionally pass a version: --php 8.2. "
            "Omit the version to auto-detect the system default."
        ),
    ),
    python: bool = typer.Option(
        False, "--python", help="Enable Python (gunicorn) support"
    ),
    python_port: int = typer.Option(
        8000, "--python-port", help="Gunicorn upstream port (used with --python)"
    ),
    nodejs: bool = typer.Option(
        False, "--nodejs", help="Enable Node.js reverse proxy support"
    ),
    node_port: int = typer.Option(
        3000,
        "--node-port",
        help="Node.js upstream port (used with --nodejs or --runtime nodejs)",
    ),
    node_socket: Optional[str] = typer.Option(
        None,
        "--node-socket",
        help="Unix Domain Socket path for Node.js (overrides --node-port)",
    ),
    runtime_opt: Optional[RuntimeMode] = typer.Option(
        None, "--runtime", help="Runtime mode (static, php, python, nodejs)"
    ),
    template: str = typer.Option(
        "default",
        "--template",
        "-t",
        help="Name of the template to use (e.g., 'wordpress')",
    ),
    mkcert: bool = typer.Option(
        False,
        "--mkcert/--no-mkcert",
        help="Generate a locally-trusted SSL certificate using mkcert",
    ),
    ssl_dir: Optional[str] = typer.Option(
        None,
        "--ssl-dir",
        help="Directory to store SSL certificates (overrides VHOST_SSL_DIR env var)",
    ),
    webroot_user: Optional[str] = typer.Option(
        None,
        "--webroot-user",
        help="Override the owner applied by chown (default: current login user)",
    ),
    webroot_group: Optional[str] = typer.Option(
        None,
        "--webroot-group",
        help="Override the group applied by chown (default: resolved web server group)",
    ),
    webroot_perms: Optional[str] = typer.Option(
        None,
        "--webroot-perms",
        help="Override directory and file permission modes. Format: '<dir_mode>:<file_mode>' (e.g. '755:644')",
    ),
    skip_permissions: bool = typer.Option(
        False,
        "--skip-permissions",
        help="Skip all webroot permission and SELinux hardening steps",
    ),
    create_dir: bool = typer.Option(
        False,
        "--create-dir",
        help="Create the document root directory if it does not exist (no prompt)",
    ),
    no_create_dir: bool = typer.Option(
        False,
        "--no-create-dir",
        help="Abort if the document root does not exist; never create it",
    ),
    scaffold: bool = typer.Option(
        False,
        "--scaffold",
        help="Generate a starter index.html in an empty document root (no prompt)",
    ),
    no_scaffold: bool = typer.Option(
        False,
        "--no-scaffold",
        help="Never generate an index.html, even after creating the document root",
    ),
):
    """Provision a new virtual host."""
    try:
        domain = validate_domain(domain)
    except ValueError as e:
        console.print(f"[red]✖[/red] {e}")
        raise typer.Exit(code=1)

    # Block creating managed vhost over an external one
    vhost_info = _find_vhost_info_for_domain(domain)
    if vhost_info and vhost_info.managed_by == "External":
        console.print(
            f"[red]✖[/red] Error: Cannot create managed vhost. Domain '{domain}' is already used by an external virtual host."
        )
        raise typer.Exit(code=1)

    # --skip-permissions mutual exclusivity check
    if skip_permissions and any(
        v is not None for v in [webroot_user, webroot_group, webroot_perms]
    ):
        console.print(
            "[red]✖[/red] Error: --skip-permissions is mutually exclusive with "
            "--webroot-user, --webroot-group, and --webroot-perms."
        )
        raise typer.Exit(code=1)

    # Validate --webroot-perms format before any filesystem changes
    resolved_dir_mode = "755"
    resolved_file_mode = "644"
    if webroot_perms is not None:
        try:
            resolved_dir_mode, resolved_file_mode = validate_webroot_perms(
                webroot_perms
            )
        except ValueError as e:
            console.print(f"[red]✖[/red] {e}")
            raise typer.Exit(code=1)

    # Validate --webroot-user / --webroot-group before any filesystem changes
    if webroot_user is not None:
        try:
            webroot_user = validate_unix_name(webroot_user, "--webroot-user")
        except ValueError as e:
            console.print(f"[red]✖[/red] {e}")
            raise typer.Exit(code=1)
    if webroot_group is not None:
        try:
            webroot_group = validate_unix_name(webroot_group, "--webroot-group")
        except ValueError as e:
            console.print(f"[red]✖[/red] {e}")
            raise typer.Exit(code=1)

    # Mutual exclusivity guard — must happen before any system writes.
    php_requested = php is not None
    active_flags = sum([php_requested, python, nodejs, runtime_opt is not None])
    if active_flags > 1:
        console.print(
            "[red]✖[/red] Error: --php, --python, --nodejs, and --runtime are mutually exclusive."
        )
        raise typer.Exit(code=1)

    # Scaffolding flag mutual exclusivity guards
    if create_dir and no_create_dir:
        console.print(
            "[red]✖[/red] Error: --create-dir and --no-create-dir are mutually exclusive."
        )
        raise typer.Exit(code=1)
    if scaffold and no_scaffold:
        console.print(
            "[red]✖[/red] Error: --scaffold and --no-scaffold are mutually exclusive."
        )
        raise typer.Exit(code=1)

    console.print(
        f"[bold blue]\\[vhost][/bold blue] Provisioning '[cyan]{domain}[/cyan]'..."
    )

    # Determine provider
    try:
        if provider is None:
            server_type = _detect_server_type()
        else:
            server_type = provider
            # Validate selected provider is installed
            if server_type == ServerType.NGINX and not is_nginx_installed():
                raise RuntimeError("Nginx is not installed.")
            if server_type == ServerType.APACHE and not is_apache_installed():
                raise RuntimeError("Apache is not installed.")
    except RuntimeError as e:
        console.print(f"  [red]✖[/red] Error: {e}")
        raise typer.Exit(code=1)

    # Non-blocking service state check
    if server_type == ServerType.NGINX:
        service_running = is_nginx_running()
        server_name = "Nginx"
    else:
        service_running = is_apache_running()
        server_name = "Apache"

    # Resolve user/group early so both directory creation and index.html use it.
    try:
        os_info_early = get_os_info()
        _detected_os_family_early = os_info_early.family
        if not _detected_os_family_early.endswith("_family"):
            _detected_os_family_early = f"{_detected_os_family_early}_family"
    except Exception:
        _detected_os_family_early = "debian_family"

    _effective_user = webroot_user or get_current_user()
    _effective_group = (
        webroot_group
        or resolve_webserver_user_group(_detected_os_family_early, server_type)[1]
    )

    # 1. Document root existence check — create interactively or via flags.
    dir_was_created = False
    if not document_root.exists():
        if no_create_dir:
            console.print(
                f"  [red]✖[/red] Document root '[cyan]{document_root}[/cyan]' does not exist.",
            )
            raise typer.Exit(code=1)

        if create_dir:
            do_create = True
        elif _is_tty():
            do_create = typer.confirm(
                f"  Directory '{document_root}' does not exist. Create it?",
                default=True,
            )
        else:
            # Non-TTY with no explicit flag: auto-create (safe default for pipelines)
            do_create = True

        if do_create:
            preflight_sudo_check()
            try:
                create_directory_privileged(
                    document_root.absolute(), _effective_user, _effective_group
                )
                dir_was_created = True
                console.print(
                    f"  [green]✔[/green] Directory '{document_root}' created "
                    f"({_effective_user}:{_effective_group}, 755)"
                )
            except RuntimeError as dir_err:
                from rich.panel import Panel as _Panel

                console.print(
                    _Panel(
                        str(dir_err),
                        title="Directory Creation Failed",
                        style="red",
                    )
                )
                raise typer.Exit(code=1)
        else:
            console.print("  [yellow]⊘[/yellow] Directory creation declined. Aborting.")
            raise typer.Exit(code=0)

    elif not document_root.is_dir():
        console.print(
            f"  [red]✖[/red] Error: '[cyan]{document_root}[/cyan]' exists but is not a directory."
        )
        raise typer.Exit(code=1)

    # 2. Scaffolding prompt — determine whether to generate index.html.
    should_scaffold = False
    if not no_scaffold:
        dir_is_empty = dir_was_created or is_directory_empty(document_root)
        if dir_is_empty:
            if scaffold:
                should_scaffold = True
            elif _is_tty():
                should_scaffold = typer.confirm(
                    "  Generate an index.html so you can test it immediately?",
                    default=True,
                )
            # Non-TTY with no --scaffold flag: safe default = do not scaffold

    # Warm the sudo credentials cache
    preflight_sudo_check()

    # Check mkcert binary availability early — before any system writes.
    if mkcert:
        try:
            check_mkcert_binary()
        except RuntimeError as e:
            console.print(f"  [red]✖[/red] {e}")
            raise typer.Exit(code=1)

    # Determine runtime mode and distro-specific PHP socket path.
    # php_fpm_version holds the resolved version string; used for service orchestration.
    php_fpm_version: Optional[str] = None
    if php is not None or runtime_opt == RuntimeMode.PHP:
        runtime = RuntimeMode.PHP
        # Determine how to resolve the socket:
        # - explicit version via --php 8.2 → strict validate
        # - --php (sentinel or no version) → auto-detect
        # - --runtime php (php is None) → legacy fallback
        if php is not None and php != _PHP_AUTO:
            # Explicit version requested: --php 8.2
            php_socket = _resolve_php_socket(php)
            php_fpm_version = php
        elif php == _PHP_AUTO:
            # --php with no version → auto-detect
            php_socket = _resolve_php_socket("")
            php_fpm_version = None
        else:
            # Legacy --runtime php path
            php_socket = _resolve_php_socket(None)
            php_fpm_version = None
        if php_socket is None:
            # Error was already printed by _resolve_php_socket
            raise typer.Exit(code=1)
    elif python or runtime_opt == RuntimeMode.PYTHON:
        runtime = RuntimeMode.PYTHON
        php_socket = None
    elif nodejs or runtime_opt == RuntimeMode.NODEJS:
        runtime = RuntimeMode.NODEJS
        php_socket = None
    elif runtime_opt == RuntimeMode.STATIC:
        runtime = RuntimeMode.STATIC
        php_socket = None
    else:
        runtime = RuntimeMode.STATIC
        php_socket = None

    hostfile_updated = False
    vhost_created = False
    provider_instance_ref = None

    try:
        # 1.5a. Generate SSL certificate if requested (before Pydantic validation
        #        so the resolved paths can be passed into the model).
        ssl_cert_path = None
        ssl_key_path = None
        if mkcert:
            resolved_ssl_dir = get_ssl_dir(ssl_dir)
            with _tracked_status(
                "[bold green]Generating SSL certificate...", spinner="dots"
            ):
                ssl_cert_path, ssl_key_path = generate_certificate(
                    domain, resolved_ssl_dir
                )
            console.print(
                f"  [green]✔[/green] SSL certificate generated ({ssl_cert_path})"
            )

        # 1.5b. Validation with Pydantic
        config = VHostConfig(
            domain=domain,
            document_root=document_root.absolute(),
            port=port,
            server_type=server_type,
            runtime=runtime,
            python_port=python_port,
            node_port=node_port,
            node_socket=node_socket,
            php_socket=php_socket,
            template=template,
            ssl_enabled=mkcert,
            cert_path=ssl_cert_path,
            key_path=ssl_key_path,
        )

        # 2. Update hostfile
        with _tracked_status("[bold green]Updating hostfile...", spinner="dots"):
            add_entry("127.0.0.1", domain)
            add_entry("127.0.0.1", get_redirect_domain(domain))
        hostfile_updated = True
        console.print(
            f"  [green]✔[/green] Hostfile entries added (127.0.0.1 {domain}, {get_redirect_domain(domain)})"
        )

        # 3. Create vhost
        provider_instance = _get_provider(server_type)
        provider_instance_ref = provider_instance
        with _tracked_status(
            f"[bold green]Configuring {server_name}...", spinner="dots"
        ):
            provider_instance.create_vhost(config, service_running=service_running)
        vhost_created = True

        console.print(f"  [green]✔[/green] {server_name} configuration generated")
        if server_type == ServerType.NGINX or (
            server_type == ServerType.APACHE
            and APACHE_SITES_ENABLED != APACHE_SITES_AVAILABLE
        ):
            console.print("  [green]✔[/green] Symbolic link created")

        # 4. PHP-FPM service orchestration (non-blocking)
        if php_socket is not None:
            _orchestrate_php_fpm_service(php_fpm_version, console)

        # 7. Generate index.html if the user confirmed scaffolding.
        if should_scaffold:
            try:
                html_content = render_index_html(
                    domain=domain,
                    provider=server_type.value,
                    document_root=str(document_root.absolute()),
                )
                write_index_html(
                    content=html_content,
                    dest_path=document_root.absolute() / "index.html",
                    user=_effective_user,
                    group=_effective_group,
                )
                console.print(
                    f"  [green]✔[/green] index.html generated "
                    f"({document_root.absolute() / 'index.html'})"
                )
            except Exception as scaffold_err:
                console.print(
                    f"  [yellow]⚠[/yellow] Could not generate index.html: {scaffold_err}"
                )

        # 5. Webroot permissions (steps 6–7 from authoritative workflow)
        if not skip_permissions:
            try:
                os_info = get_os_info()
                os_family = os_info.family
                if not os_family.endswith("_family"):
                    os_family = f"{os_family}_family"
            except Exception:
                os_family = "debian_family"

            effective_user = webroot_user or get_current_user()
            effective_group = (
                webroot_group or resolve_webserver_user_group(os_family, server_type)[1]
            )

            with _tracked_status(
                "[bold green]Applying webroot permissions...", spinner="dots"
            ):
                apply_webroot_permissions(
                    document_root.absolute(),
                    effective_user,
                    effective_group,
                    dir_mode=resolved_dir_mode,
                    file_mode=resolved_file_mode,
                )
            console.print(
                f"  [green]✔[/green] Webroot permissions set "
                f"({effective_user}:{effective_group}, dirs={resolved_dir_mode}, files={resolved_file_mode}, SetGID)"
            )

            # 6. SELinux context hardening (RHEL/Fedora only)
            if os_family == "rhel_family" and is_selinux_active():
                with _tracked_status(
                    "[bold green]Applying SELinux context...", spinner="dots"
                ):
                    apply_selinux_webroot_context(document_root.absolute())
                console.print(
                    "  [green]✔[/green] SELinux context applied (httpd_sys_content_t)"
                )

        if service_running:
            console.print(f"  [green]✔[/green] {server_name} configuration valid")
            console.print(f"  [green]✔[/green] {server_name} reloaded successfully")
            scheme = "https" if mkcert else "http"
            console.print(
                f"\n✨ [bold green]Success![/bold green] Your site is live at: [underline green]{scheme}://{domain}[/underline green]"
            )
        else:
            console.print(
                f"  [dim]⊘ Skipped: {server_name} configuration validation ({server_name} service is not running)[/dim]"
            )
            console.print(
                f"  [dim]⊘ Skipped: {server_name} reload ({server_name} service is not running)[/dim]"
            )
            console.print(
                f"\n✨ [bold green]Virtual host '{domain}' created successfully![/bold green]"
            )
            console.print(
                f"\n[yellow]⚠  Notification: {server_name} is installed but not running. "
                f"You must start the service manually to apply these changes.[/yellow]"
            )

    except Exception as e:
        if vhost_created and provider_instance_ref is not None:
            try:
                provider_instance_ref.remove_vhost(domain, service_running=False)
                console.print(
                    "  [yellow]↪[/yellow] Rollback: Vhost configuration removed due to failure."
                )
            except Exception as rollback_err:
                console.print(
                    f"  [red]✖[/red] Error during vhost rollback: {rollback_err}"
                )

        if hostfile_updated:
            try:
                remove_entry(domain)
                remove_entry(get_redirect_domain(domain))
                console.print(
                    "  [yellow]↪[/yellow] Rollback: Hostfile entry removed due to failure."
                )
            except Exception as rollback_err:
                console.print(
                    f"  [red]✖[/red] Error during hostfile rollback: {rollback_err}"
                )

        console.print(f"  [red]✖[/red] Error: {e}")
        raise typer.Exit(code=1)


def _resolve_php_socket(php_version: Optional[str] = None) -> Optional[str]:
    """Resolve and validate the PHP-FPM socket path.

    Three calling modes are supported:
    - ``php_version=None``: Legacy / ``--runtime php`` path — look up the
      OS-specific socket from ``PHP_SOCKET_PATHS`` (backward-compatible).
    - ``php_version=""``: New auto-detect path (``--php`` flag with no version)
      — call ``detect_default_version`` and construct the socket path.
    - ``php_version="8.2"`` etc: Explicit version (``--php 8.2``) — call
      ``validate_version_present`` with strict error on failure.

    Returns:
        Resolved absolute socket path string, or ``None`` if resolution failed
        (error already printed to the console by this function).
    """
    try:
        os_info = get_os_info()
        os_family = os_info.family
        if not os_family.endswith("_family"):
            os_family = f"{os_family}_family"
    except Exception:
        os_family = "debian_family"

    if php_version is None:
        # Legacy path: use the OS-family→socket-path table, no validation.
        return PHP_SOCKET_PATHS.get(os_family, DEFAULT_PHP_SOCKET)

    try:
        if php_version == "":
            # Auto-detect: find the highest installed version.
            version = detect_default_version(os_family)
            return resolve_socket_path(version, os_family)
        else:
            # Explicit version requested — validate presence strictly.
            return validate_version_present(php_version, os_family)
    except PhpFpmNotFoundError as exc:
        console.print(
            Panel(
                str(exc),
                title="PHP-FPM Not Found",
                style="red",
            )
        )
        return None


def _orchestrate_php_fpm_service(php_version: Optional[str], _console: Console) -> None:
    """Attempt to start the PHP-FPM service, printing a warning on failure.

    This function is intentionally non-blocking: a failure produces a Rich
    warning panel but does not raise an exception.

    Args:
        php_version: Resolved version string or ``None`` for auto-detected version.
        _console: Rich console instance for output.
    """
    try:
        os_info = get_os_info()
        os_family = os_info.family
        if not os_family.endswith("_family"):
            os_family = f"{os_family}_family"
    except Exception:
        os_family = "debian_family"

    if php_version is None or php_version == "":
        try:
            php_version = detect_default_version(os_family)
        except PhpFpmNotFoundError:
            return

    warning = start_service(php_version, os_family)
    if warning:
        _console.print(
            Panel(
                str(warning),
                title="PHP-FPM Service Warning",
                style="yellow",
            )
        )


@app.command()
def enable(
    domain: str = typer.Argument(..., help="The domain name to enable"),
    provider: Optional[ServerType] = typer.Option(
        None, "--provider", "-p", help="Web server provider (nginx or apache)"
    ),
):
    """Enable an existing virtual host."""
    try:
        domain = validate_domain(domain)
    except ValueError as e:
        console.print(f"[red]✖[/red] {e}")
        raise typer.Exit(code=1)

    console.print(
        f"[bold blue]\\[vhost][/bold blue] Enabling '[cyan]{domain}[/cyan]'..."
    )

    # Warm the sudo credentials cache
    preflight_sudo_check()

    vhost_info = _find_vhost_info_for_domain(domain, provider)

    if vhost_info is None:
        console.print(
            f"  [red]✖[/red] Error: No configuration found for [cyan]{domain}[/cyan]."
        )
        raise typer.Exit(code=1)

    if vhost_info.status == "Enabled":
        console.print(
            f"  [yellow]⊘[/yellow] Virtual host '[cyan]{domain}[/cyan]' is already enabled."
        )
        return

    server_type = vhost_info.server_type

    if server_type == ServerType.NGINX:
        service_running = is_nginx_running()
        server_name = "Nginx"
    else:
        service_running = is_apache_running()
        server_name = "Apache"

    try:
        with _tracked_status("[bold green]Updating hostfile...", spinner="dots"):
            add_entry("127.0.0.1", domain)
            add_entry("127.0.0.1", get_redirect_domain(domain))
        console.print(
            f"  [green]✔[/green] Hostfile entries added (127.0.0.1 {domain}, {get_redirect_domain(domain)})"
        )
    except Exception as e:
        console.print(f"  [yellow]⚠[/yellow] Could not update hostfile: {e}")

    try:
        provider_instance = _get_provider(server_type)
        with _tracked_status(
            f"[bold green]Enabling {server_name} configuration...", spinner="dots"
        ):
            # Pass the actual config_path so the provider knows where the file
            # currently lives (e.g. conf.disabled/ on Fedora after a disable).
            provider_instance.enable_vhost(
                vhost_info.config_path, service_running=service_running
            )
        console.print(f"  [green]✔[/green] {server_name} configuration enabled")

        if service_running:
            console.print(f"  [green]✔[/green] {server_name} reloaded successfully")
        else:
            console.print(
                f"  [dim]⊘ Skipped: {server_name} reload ({server_name} service is not running)[/dim]"
            )

        console.print(
            f"\n✅ [bold green]Success![/bold green] Virtual host '[cyan]{domain}[/cyan]' has been enabled."
        )

        if not service_running:
            console.print(
                f"\n[yellow]⚠  Notification: {server_name} is installed but not running. "
                f"You must start the service manually to apply these changes.[/yellow]"
            )

    except Exception as e:
        console.print(f"  [red]✖[/red] Error: {e}")
        raise typer.Exit(code=1)


@app.command()
def disable(
    domain: str = typer.Argument(..., help="The domain name to disable"),
    provider: Optional[ServerType] = typer.Option(
        None, "--provider", "-p", help="Web server provider (nginx or apache)"
    ),
):
    """Disable an existing virtual host."""
    try:
        domain = validate_domain(domain)
    except ValueError as e:
        console.print(f"[red]✖[/red] {e}")
        raise typer.Exit(code=1)

    console.print(
        f"[bold blue]\\[vhost][/bold blue] Disabling '[cyan]{domain}[/cyan]'..."
    )

    # Warm the sudo credentials cache
    preflight_sudo_check()

    vhost_info = _find_vhost_info_for_domain(domain, provider)

    if vhost_info is None:
        console.print(
            f"  [red]✖[/red] Error: No configuration found for [cyan]{domain}[/cyan]."
        )
        raise typer.Exit(code=1)

    if vhost_info.status == "Disabled":
        console.print(
            f"  [yellow]⊘[/yellow] Virtual host '[cyan]{domain}[/cyan]' is already disabled."
        )
        return

    server_type = vhost_info.server_type

    if server_type == ServerType.NGINX:
        service_running = is_nginx_running()
        server_name = "Nginx"
    else:
        service_running = is_apache_running()
        server_name = "Apache"

    try:
        with _tracked_status("[bold green]Updating hostfile...", spinner="dots"):
            remove_entry(domain)
            remove_entry(get_redirect_domain(domain))
        console.print("  [green]✔[/green] Hostfile entries removed")
    except Exception as e:
        console.print(f"  [yellow]⚠[/yellow] Could not update hostfile: {e}")

    try:
        provider_instance = _get_provider(server_type)
        with _tracked_status(
            f"[bold green]Disabling {server_name} configuration...", spinner="dots"
        ):
            provider_instance.disable_vhost(domain, service_running=service_running)
        console.print(f"  [green]✔[/green] {server_name} configuration disabled")

        if service_running:
            console.print(f"  [green]✔[/green] {server_name} reloaded successfully")
        else:
            console.print(
                f"  [dim]⊘ Skipped: {server_name} reload ({server_name} service is not running)[/dim]"
            )

        console.print(
            f"\n🚫 [bold green]Success![/bold green] Virtual host '[cyan]{domain}[/cyan]' has been disabled."
        )

        if not service_running:
            console.print(
                f"\n[yellow]⚠  Notification: {server_name} is installed but not running. "
                f"You must start the service manually to apply these changes.[/yellow]"
            )

    except Exception as e:
        console.print(f"  [red]✖[/red] Error: {e}")
        raise typer.Exit(code=1)


@app.command(name="list")
def list_vhosts(
    provider: Optional[ServerType] = typer.Option(
        None, "--provider", "-p", help="Filter by web server provider (nginx or apache)"
    ),
):
    """List all virtual hosts on the system."""
    all_vhosts: List[VHostInfo] = _scan_all_vhosts_locally(filter_provider=provider)

    if not all_vhosts:
        console.print("[yellow]No virtual hosts found.[/yellow]")
        return

    # Sort for consistent output
    all_vhosts.sort(key=lambda x: (x.domain, x.server_type.value))

    table = Table(
        title="System Virtual Hosts",
        show_header=True,
        header_style="bold magenta",
        highlight=True,
    )
    table.add_column("Domain", style="cyan", overflow="fold")
    table.add_column("Server", style="green", overflow="fold")
    table.add_column("Status", overflow="fold")
    table.add_column("Managed By", overflow="fold")
    table.add_column("Document Root", style="dim", overflow="fold")
    table.add_column("Config Path", style="dim", overflow="fold")

    for vhost in all_vhosts:
        status_style = "green" if vhost.status == "Enabled" else "yellow"
        managed_by_style = "blue" if vhost.managed_by == "VHost Helper" else "dim"

        table.add_row(
            vhost.domain,
            vhost.server_type.value.capitalize(),
            f"[{status_style}]{vhost.status}[/{status_style}]",
            f"[{managed_by_style}]{vhost.managed_by}[/{managed_by_style}]",
            str(vhost.document_root) if vhost.document_root else "—",
            str(vhost.config_path),
        )

    console.print(table)


@app.command()
def info(
    domain: Optional[str] = typer.Argument(
        None, help="The domain name to show info for"
    ),
    provider: Optional[ServerType] = typer.Option(
        None, "--provider", "-p", help="Web server provider (nginx or apache)"
    ),
):
    """Display detailed info for a domain or system information."""
    if not domain:
        try:
            os_info = get_os_info()
            console.print("System Info:")
            console.print(f"OS: {os_info.id} {os_info.version}")
            console.print(f"Nginx installed: {is_nginx_installed()}")
            console.print(f"Apache installed: {is_apache_installed()}")
        except Exception as e:
            console.print(f"Error detecting OS: {e}")
        return

    try:
        domain = validate_domain(domain)
    except ValueError as e:
        console.print(f"[red]✖[/red] {e}")
        raise typer.Exit(code=1)

    vhost_info = _find_vhost_info_for_domain(domain, provider)

    if vhost_info is None:
        console.print(f"[red]✖[/red] No configuration found for [cyan]{domain}[/cyan].")
        raise typer.Exit(code=1)

    # Extract port from config file for display
    port_str = ""
    try:
        config_content = vhost_info.config_path.read_text()
        if vhost_info.server_type == ServerType.NGINX:
            port_match = re.search(r"listen\s+(\d+)", config_content)
        else:
            port_match = re.search(
                r"<VirtualHost[^>]*:(\d+)>", config_content, re.IGNORECASE
            )
            if not port_match:
                port_match = re.search(
                    r"^\s*listen\s+(\d+)", config_content, re.IGNORECASE | re.MULTILINE
                )
        if port_match:
            port_str = port_match.group(1)
    except Exception:
        pass

    table = Table(title=f"Info for {domain}", show_header=False, box=None)
    table.add_column(style="bold magenta")
    table.add_column()

    table.add_row("Domain:", vhost_info.domain)
    table.add_row("Server:", vhost_info.server_type.value.capitalize())
    table.add_row("Status:", vhost_info.status)
    table.add_row("Managed By:", vhost_info.managed_by)
    if port_str:
        table.add_row("Port:", port_str)
    table.add_row("Config Path:", str(vhost_info.config_path))
    if vhost_info.document_root:
        table.add_row("Document Root:", str(vhost_info.document_root))

    console.print(table)


@app.command()
def remove(
    domain: str = typer.Argument(..., help="The domain name to remove"),
    provider: Optional[ServerType] = typer.Option(
        None, "--provider", "-p", help="Web server provider (nginx or apache)"
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation"),
):
    """Tear down an existing virtual host."""
    try:
        domain = validate_domain(domain)
    except ValueError as e:
        console.print(f"[red]✖[/red] {e}")
        raise typer.Exit(code=1)

    if not force:
        confirm = typer.confirm(f"Are you sure you want to remove {domain}?")
        if not confirm:
            console.print("[yellow]Aborted.[/yellow]")
            raise typer.Abort()

    console.print(
        f"[bold blue]\\[vhost][/bold blue] Removing '[cyan]{domain}[/cyan]'..."
    )

    # Warm the sudo credentials cache
    preflight_sudo_check()

    # Determine provider
    if provider is None:
        server_type = _detect_provider_for_domain(domain)
        if server_type is None:
            console.print(
                f"  [red]✖[/red] Error: No configuration found for [cyan]{domain}[/cyan]"
            )
            raise typer.Exit(code=1)
    else:
        server_type = provider

    if server_type == ServerType.NGINX:
        service_running = is_nginx_running()
        server_name = "Nginx"
    else:
        service_running = is_apache_running()
        server_name = "Apache"

    try:
        # 1. Remove hostfile entry
        with _tracked_status("[bold green]Updating hostfile...", spinner="dots"):
            remove_entry(domain)
            remove_entry(get_redirect_domain(domain))
        console.print("  [green]✔[/green] Hostfile entries removed")

        # 2. Remove vhost
        provider_instance = _get_provider(server_type)
        with _tracked_status(
            f"[bold green]Removing {server_name} configuration...", spinner="dots"
        ):
            provider_instance.remove_vhost(domain, service_running=service_running)
        console.print(f"  [green]✔[/green] {server_name} configuration removed")

        if service_running:
            console.print(f"  [green]✔[/green] {server_name} reloaded successfully")
        else:
            console.print(
                f"  [dim]⊘ Skipped: {server_name} reload ({server_name} service is not running)[/dim]"
            )

        console.print(
            f"\n🗑️  [bold green]Success![/bold green] Virtual host '[cyan]{domain}[/cyan]' has been removed."
        )

        if not service_running:
            console.print(
                f"\n[yellow]⚠  Notification: {server_name} is installed but not running. "
                f"You must start the service manually to apply these changes.[/yellow]"
            )

    except Exception as e:
        console.print(f"  [red]✖[/red] Error: {e}")
        raise typer.Exit(code=1)


@app.command(name="template-vars")
def template_vars():
    """Show all Jinja2 variables available in vhost templates."""
    console.print(
        Panel(
            "[bold]These variables are injected into every [cyan].conf.j2[/cyan] template "
            "at render time.[/bold]\n"
            "Place custom templates in [green]~/.vhost-helper/templates/nginx/[/green] "
            "or [green]~/.vhost-helper/templates/apache/[/green].\n"
            "Any variable listed below can be used with the [cyan]{{ variable }}[/cyan] syntax.",
            title="[bold cyan]VHost Helper — Template Variable Reference[/bold cyan]",
            border_style="cyan",
        )
    )

    table = Table(
        show_header=True,
        header_style="bold magenta",
        border_style="dim",
        box=None,
        padding=(0, 2),
    )
    table.add_column("Variable", style="bold cyan", no_wrap=True)
    table.add_column("Type", style="yellow", no_wrap=True)
    table.add_column("Default", style="green")
    table.add_column("Providers", style="blue", no_wrap=True)
    table.add_column("Description")

    rows = [
        (
            "domain",
            "str",
            "required",
            "nginx, apache",
            "Primary domain name (e.g. [cyan]example.com[/cyan])",
        ),
        (
            "document_root",
            "str",
            "required",
            "nginx, apache",
            "Absolute path to the web root directory",
        ),
        (
            "port",
            "int",
            "[green]80[/green]",
            "nginx, apache",
            "TCP port the server listens on",
        ),
        (
            "runtime",
            "str",
            "[green]static[/green]",
            "nginx, apache",
            "Runtime mode: [bold]static[/bold] | [bold]php[/bold] | [bold]python[/bold] | [bold]nodejs[/bold]",
        ),
        (
            "python_port",
            "int",
            "[green]8000[/green]",
            "nginx, apache",
            "Local port gunicorn/uvicorn listens on (used when runtime=python)",
        ),
        (
            "node_port",
            "int",
            "[green]3000[/green]",
            "nginx, apache",
            "Local port the Node.js process listens on (used when runtime=nodejs)",
        ),
        (
            "node_socket",
            "str",
            "[green]None[/green]",
            "nginx, apache",
            "Unix Domain Socket path for Node.js — overrides node_port when set",
        ),
        (
            "php_socket",
            "str",
            "[green]/run/php/php-fpm.sock[/green]",
            "nginx, apache",
            "PHP-FPM unix socket path (used when runtime=php)",
        ),
        (
            "os_family",
            "str",
            "auto-detected",
            "nginx, apache",
            "OS family: [bold]debian_family[/bold] | [bold]rhel_family[/bold] (controls provider-specific config paths)",
        ),
    ]

    for row in rows:
        table.add_row(*row)

    console.print(table)

    console.print()
    console.print("[bold]Runtime values[/bold]")
    runtime_table = Table(
        show_header=True,
        header_style="bold",
        border_style="dim",
        box=None,
        padding=(0, 2),
    )
    runtime_table.add_column("Value", style="bold cyan", no_wrap=True)
    runtime_table.add_column("Behaviour")
    runtime_table.add_row(
        "static", "Serves static files; try_files with index.html fallback"
    )
    runtime_table.add_row("php", "FastCGI pass to [green]{{ php_socket }}[/green]")
    runtime_table.add_row(
        "python", "Reverse proxy to [green]http://127.0.0.1:{{ python_port }}[/green]"
    )
    runtime_table.add_row(
        "nodejs",
        "Reverse proxy to [green]http://127.0.0.1:{{ node_port }}[/green] (or [green]{{ node_socket }}[/green] when set)",
    )
    console.print(runtime_table)

    console.print()
    console.print(
        "[dim]Tip: run [bold]vhost create --help[/bold] to see which CLI flags map to each variable.[/dim]"
    )


# ---------------------------------------------------------------------------
# `vhost templates` sub-commands
# ---------------------------------------------------------------------------


@templates_app.command("list")
def templates_list(
    provider: Optional[str] = typer.Option(
        None, "--provider", help="Filter by provider (nginx or apache)"
    ),
):
    """List all available Jinja2 templates."""
    templates_dir = APP_TEMPLATES_DIR
    result = list_templates(templates_dir, provider=provider)

    if not result:
        if provider:
            console.print(
                f"[red]✖[/red] No templates found for provider '[cyan]{provider}[/cyan]'."
            )
        else:
            console.print("[red]✖[/red] No templates found.")
        raise typer.Exit(code=1)

    table = Table(
        title="Available Templates", show_header=True, header_style="bold magenta"
    )
    table.add_column("Provider", style="cyan", no_wrap=True)
    table.add_column("Template", style="green")

    for prov, modes in result.items():
        for mode in modes:
            table.add_row(prov, mode)

    console.print(table)


@templates_app.command("inspect")
def templates_inspect(
    name: str = typer.Argument(
        ..., help="Template name in <provider>-<mode> format (e.g. nginx-php)"
    ),
):
    """Show variables exposed by a specific template."""
    templates_dir = APP_TEMPLATES_DIR
    template_path = resolve_template_path(name, templates_dir)

    if template_path is None:
        console.print(
            f"[red]✖[/red] Template '[cyan]{name}[/cyan]' not found in {templates_dir}."
        )
        raise typer.Exit(code=1)

    variables = extract_variables(template_path)
    metadata = extract_metadata(template_path)

    table = Table(
        title=f"Template: {name}",
        show_header=True,
        header_style="bold magenta",
        border_style="dim",
    )
    table.add_column("Variable", style="bold cyan", no_wrap=True)
    table.add_column("Description")
    table.add_column("Default", style="green")

    for var in variables:
        meta = metadata.get(var, {})
        description = meta.get("description", "")
        default = str(meta.get("default")) if meta.get("default") is not None else "—"
        table.add_row(var, description, default)

    console.print(table)


@app.command()
def logs(
    domain: str = typer.Argument(..., help="The domain name to tail logs for"),
    provider: Optional[ServerType] = typer.Option(
        None, "--provider", "-p", help="Web server provider (nginx or apache)"
    ),
    error: bool = typer.Option(False, "--error", help="Tail only the error log"),
    access: bool = typer.Option(False, "--access", help="Tail only the access log"),
):
    """Tail live log files for a virtual host."""
    if error and access:
        console.print(
            "[red]✖[/red] Error: --error and --access are mutually exclusive. "
            "Use one flag at a time."
        )
        raise typer.Exit(code=1)

    try:
        domain = validate_domain(domain)
    except ValueError as e:
        console.print(f"[red]✖[/red] {e}")
        raise typer.Exit(code=1)

    # Determine provider (reuses existing auto-detection cascade)
    if provider is None:
        server_type = _detect_provider_for_domain(domain)
        if server_type is None:
            console.print(
                f"[red]✖[/red] Error: VHost not found or disabled: '{domain}'"
            )
            raise typer.Exit(code=1)
    else:
        server_type = provider

    # Determine the enabled-sites path for this provider/domain
    if server_type == ServerType.NGINX:
        enabled_path = NGINX_SITES_ENABLED / (domain + ".conf")
    else:
        enabled_path = APACHE_SITES_ENABLED / (domain + ".conf")

    # F4 Step 1: VHost enabled check
    if not enabled_path.exists() and not enabled_path.is_symlink():
        console.print(f"[red]✖[/red] Error: VHost not found or disabled: '{domain}'")
        raise typer.Exit(code=1)

    # Read config — resolve symlink so we always get real file content
    try:
        config_file = enabled_path.resolve()
        config_content = config_file.read_text()
    except (PermissionError, OSError) as e:
        console.print(f"[red]✖[/red] Error reading configuration: {e}")
        raise typer.Exit(code=1)

    # F4 Step 2: Extract log paths from configuration
    if server_type == ServerType.NGINX:
        access_log_path, error_log_path = extract_nginx_log_paths(config_content)
    else:
        access_log_path, error_log_path = extract_apache_log_paths(config_content)

    # Determine which paths to tail based on flags
    if error:
        if error_log_path is None:
            console.print(
                f"[red]✖[/red] Error: No log paths found in configuration for '{domain}'"
            )
            raise typer.Exit(code=1)
        paths_to_tail = [error_log_path]
    elif access:
        if access_log_path is None:
            console.print(
                f"[red]✖[/red] Error: No log paths found in configuration for '{domain}'"
            )
            raise typer.Exit(code=1)
        paths_to_tail = [access_log_path]
    else:
        if access_log_path is None and error_log_path is None:
            console.print(
                f"[red]✖[/red] Error: No log paths found in configuration for '{domain}'"
            )
            raise typer.Exit(code=1)
        paths_to_tail = [p for p in [access_log_path, error_log_path] if p is not None]

    # F4 Step 3: Filesystem existence check
    for path in paths_to_tail:
        if not os.path.isfile(path):
            console.print(f"[red]✖[/red] Error: Log file not found at '{path}'")
            raise typer.Exit(code=1)

    # F5: Resolve tail binary and launch
    tail_bin = shutil.which("tail")
    if tail_bin is None:
        console.print("[red]✖[/red] Error: 'tail' binary not found on PATH")
        raise typer.Exit(code=1)

    cmd = [tail_bin, "-f"] + paths_to_tail
    try:
        proc = subprocess.Popen(cmd, shell=False)
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        raise typer.Exit(code=0)


def _normalize_php_argv(argv: list) -> list:
    """Preprocess argv so ``--php`` without a version injects the auto-detect sentinel.

    This allows the real CLI to accept both ``--php`` (auto-detect) and
    ``--php 8.2`` (explicit version) even though Typer's ``Optional[str]``
    option always requires a value.
    """
    result = []
    i = 0
    while i < len(argv):
        if argv[i] == "--php":
            result.append("--php")
            # If the next token looks like a PHP version (digits.digits), keep it.
            if i + 1 < len(argv) and re.match(r"^\d+\.\d+$", argv[i + 1]):
                i += 1
                result.append(argv[i])
            else:
                result.append(_PHP_AUTO)
        else:
            result.append(argv[i])
        i += 1
    return result


def run() -> None:
    """Entry point for the installed CLI binary.

    Preprocesses ``sys.argv`` to support ``--php [VERSION]`` optional-value
    syntax before handing off to the Typer application.
    """
    import sys

    sys.argv = _normalize_php_argv(sys.argv)
    app()


if __name__ == "__main__":
    run()
