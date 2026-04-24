import typer
import re
from contextlib import contextmanager
from typing import Optional, Union
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
)
from .hostfile import add_entry, remove_entry
from .providers.nginx import NginxProvider, is_nginx_installed, is_nginx_running
from .providers.apache import ApacheProvider, is_apache_installed, is_apache_running
from .os_detector import get_os_info
from .utils import set_active_live, preflight_sudo_check
from .template_inspector import (
    list_templates,
    resolve_template_path,
    extract_variables,
    extract_metadata,
)

app = typer.Typer(help="VHost Helper: Unified virtual host management.")
templates_app = typer.Typer(help="Inspect and list available Jinja2 templates.")
app.add_typer(templates_app, name="templates")
console = Console()

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
    php: bool = typer.Option(False, "--php", help="Enable PHP (php-fpm) support"),
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
):
    """Provision a new virtual host."""
    try:
        domain = validate_domain(domain)
    except ValueError as e:
        console.print(f"[red]✖[/red] {e}")
        raise typer.Exit(code=1)

    # Mutual exclusivity guard — must happen before any system writes.
    active_flags = sum([php, python, nodejs, runtime_opt is not None])
    if active_flags > 1:
        console.print(
            "[red]✖[/red] Error: --php, --python, --nodejs, and --runtime are mutually exclusive."
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

    # 1. Validation
    if not document_root.exists():
        console.print(
            f"  [red]✖[/red] Error: Document root [cyan]{document_root}[/cyan] does not exist."
        )
        raise typer.Exit(code=1)

    # Warm the sudo credentials cache
    preflight_sudo_check()

    # Determine runtime mode and distro-specific PHP socket path.
    if php or runtime_opt == RuntimeMode.PHP:
        runtime = RuntimeMode.PHP
        php_socket = _resolve_php_socket()
    elif python or runtime_opt == RuntimeMode.PYTHON:
        runtime = RuntimeMode.PYTHON
        php_socket = DEFAULT_PHP_SOCKET  # not used, set to default for model validity
    elif nodejs or runtime_opt == RuntimeMode.NODEJS:
        runtime = RuntimeMode.NODEJS
        php_socket = DEFAULT_PHP_SOCKET  # not used, set to default for model validity
    elif runtime_opt == RuntimeMode.STATIC:
        runtime = RuntimeMode.STATIC
        php_socket = DEFAULT_PHP_SOCKET
    else:
        runtime = RuntimeMode.STATIC
        php_socket = DEFAULT_PHP_SOCKET

    hostfile_updated = False

    try:
        # 1.5. Validation with Pydantic
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
        with _tracked_status(
            f"[bold green]Configuring {server_name}...", spinner="dots"
        ):
            provider_instance.create_vhost(config, service_running=service_running)

        console.print(f"  [green]✔[/green] {server_name} configuration generated")
        if server_type == ServerType.NGINX or (
            server_type == ServerType.APACHE
            and APACHE_SITES_ENABLED != APACHE_SITES_AVAILABLE
        ):
            console.print("  [green]✔[/green] Symbolic link created")

        if service_running:
            console.print(f"  [green]✔[/green] {server_name} configuration valid")
            console.print(f"  [green]✔[/green] {server_name} reloaded successfully")
            scheme = "http"
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


def _resolve_php_socket() -> str:
    """Return the distro-appropriate php-fpm socket path, falling back to the parent default."""
    try:
        os_info = get_os_info()
        return PHP_SOCKET_PATHS.get(os_info.family, DEFAULT_PHP_SOCKET)
    except Exception:
        return DEFAULT_PHP_SOCKET


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
        enabled_path = NGINX_SITES_ENABLED / (domain + ".conf")
    else:
        service_running = is_apache_running()
        server_name = "Apache"
        enabled_path = APACHE_SITES_ENABLED / (domain + ".conf")

    if enabled_path.exists():
        console.print(
            f"  [yellow]ℹ[/yellow] Virtual host '{domain}' is already enabled for {server_name}."
        )
        raise typer.Exit()

    try:
        with _tracked_status("[bold green]Updating hostfile...", spinner="dots"):
            add_entry("127.0.0.1", domain)
            add_entry("127.0.0.1", get_redirect_domain(domain))
        console.print(
            f"  [green]✔[/green] Hostfile entries added (127.0.0.1 {domain}, {get_redirect_domain(domain)})"
        )

        provider_instance = _get_provider(server_type)
        with _tracked_status(
            f"[bold green]Enabling {server_name} configuration...", spinner="dots"
        ):
            provider_instance.enable_vhost(domain, service_running=service_running)
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
        enabled_path = NGINX_SITES_ENABLED / (domain + ".conf")
    else:
        service_running = is_apache_running()
        server_name = "Apache"
        enabled_path = APACHE_SITES_ENABLED / (domain + ".conf")

    if not enabled_path.exists() and not enabled_path.is_symlink():
        console.print(
            f"  [yellow]ℹ[/yellow] Virtual host '{domain}' is already disabled for {server_name}."
        )
        raise typer.Exit()

    try:
        with _tracked_status("[bold green]Updating hostfile...", spinner="dots"):
            remove_entry(domain)
            remove_entry(get_redirect_domain(domain))
        console.print("  [green]✔[/green] Hostfile entries removed")

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


@app.command()
def list():
    """List all managed virtual hosts."""
    table = Table(title="Managed Virtual Hosts")
    table.add_column("Domain", style="cyan")
    table.add_column("Path", style="magenta")
    table.add_column("Server", style="green")
    table.add_column("Status", style="bold")

    # Helper to process a directory
    def process_dir(directory: Path, server_name: str, enabled_dir: Path):
        if directory.exists():
            for config_file in directory.iterdir():
                if config_file.is_file() and config_file.suffix == ".conf":
                    domain = config_file.stem
                    try:
                        validate_domain(domain)
                    except ValueError:
                        continue

                    root_path = "N/A"
                    try:
                        content = config_file.read_text()
                        if server_name == "Nginx":
                            root_match = re.search(r'root\s+"?([^";]+)"?;', content)
                        else:  # Apache
                            root_match = re.search(
                                r'DocumentRoot\s+"?([^"\s>]+)"?', content
                            )

                        if root_match:
                            root_path = root_match.group(1)
                    except (PermissionError, Exception):
                        root_path = "[red]Permission Denied[/red]"

                    enabled_link = enabled_dir / (domain + ".conf")
                    status = (
                        "[green]Enabled[/green]"
                        if enabled_link.exists()
                        else "[yellow]Disabled[/yellow]"
                    )
                    table.add_row(domain, root_path, server_name, status)

    process_dir(NGINX_SITES_AVAILABLE, "Nginx", NGINX_SITES_ENABLED)
    process_dir(APACHE_SITES_AVAILABLE, "Apache", APACHE_SITES_ENABLED)

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
    if domain:
        try:
            domain = validate_domain(domain)
        except ValueError as e:
            console.print(f"[red]✖[/red] {e}")
            raise typer.Exit(code=1)

        # Determine provider
        if provider is None:
            server_type = _detect_provider_for_domain(domain)
            if server_type is None:
                console.print(
                    f"[red]✖[/red] Error: No configuration found for [cyan]{domain}[/cyan]"
                )
                raise typer.Exit(code=1)
        else:
            server_type = provider

        if server_type == ServerType.NGINX:
            config_path = NGINX_SITES_AVAILABLE / (domain + ".conf")
            enabled_dir = NGINX_SITES_ENABLED
            server_name_display = "Nginx"
        else:
            config_path = APACHE_SITES_AVAILABLE / (domain + ".conf")
            enabled_dir = APACHE_SITES_ENABLED
            server_name_display = "Apache"

        if config_path.exists():
            try:
                content = config_path.read_text()
                if server_type == ServerType.NGINX:
                    server_name_match = re.search(r"server_name\s+(.+?);", content)
                    root_match = re.search(r'root\s+"?([^";]+)"?;', content)
                    listen_match = re.search(r"listen\s+(.+?);", content)
                else:  # Apache
                    server_name_match = re.search(r"ServerName\s+(.+?)[\s\n]", content)
                    root_match = re.search(r'DocumentRoot\s+"?([^"\s>]+)"?', content)
                    listen_match = re.search(r"<VirtualHost\s+[^:]+:(\d+)>", content)

                server_name = (
                    server_name_match.group(1).replace('"', "")
                    if server_name_match
                    else domain
                )
                root_path = root_match.group(1) if root_match else "Unknown"
                port_info = (
                    listen_match.group(1).replace('"', "") if listen_match else "80"
                )

                enabled_link = enabled_dir / (domain + ".conf")
                link_status = "Enabled" if enabled_link.exists() else "Disabled"

                info_text = (
                    f"Domain Name: [bold cyan]{server_name}[/bold cyan]\n"
                    f"Document Root: [bold green]{root_path}[/bold green]\n"
                    f"Server Type: [bold green]{server_name_display}[/bold green]\n"
                    f"Server Port/Listen: [bold yellow]{port_info}[/bold yellow]\n"
                    f"Status: [bold]{link_status}[/bold]"
                )
                console.print(Panel(info_text, title=f"Virtual Host Info: {domain}"))
            except PermissionError:
                console.print(
                    "[red]✖[/red] Error: Permission denied reading configuration. Try running with [bold]sudo[/bold]."
                )
            except Exception as e:
                console.print(f"[red]✖[/red] Error reading configuration: {e}")
        else:
            console.print(
                f"[red]✖[/red] Error: No configuration found for [cyan]{domain}[/cyan]"
            )
            raise typer.Exit(code=1)
    else:
        try:
            os_info = get_os_info()
            console.print(
                Panel(
                    f"ID: [bold cyan]{os_info.id}[/bold cyan]\n"
                    f"Version: [bold cyan]{os_info.version}[/bold cyan]\n"
                    f"Family: [bold cyan]{os_info.family}[/bold cyan]\n"
                    f"Nginx Installed: {'[green]Yes[/green]' if is_nginx_installed() else '[red]No[/red]'}\n"
                    f"Apache Installed: {'[green]Yes[/green]' if is_apache_installed() else '[red]No[/red]'}",
                    title="System Information",
                )
            )
        except Exception as e:
            console.print(f"[red]Error detecting OS: {e}[/red]")


@app.command("template-vars")
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


if __name__ == "__main__":
    app()
