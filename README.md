# Ultimate VHost Helper

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-196%20passing-brightgreen)](scripts/run_integration_tests.sh)
[![Distros](https://img.shields.io/badge/distros-Ubuntu%20%7C%20Fedora-orange)]()
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)]()

> A professional, modular CLI tool for automated virtual host management across Linux distributions.

**Version**: v0.3 | **Status**: Stable | **Supported Distros**: Debian/Ubuntu, Fedora/RHEL

## Why?
Managing virtual hosts manually is a repetitive and error-prone process involving sensitive modifications to `/etc/hosts` and web server configurations. **VHost Helper** eliminates this friction by providing a unified, distribution-agnostic CLI that automates provisioning, security hardening, and service management with atomic precision.

Whether you are a local developer spinning up projects on `.test` domains or a sysadmin managing production nodes, VHost Helper ensures your configurations are consistent, secure, and compliant with best practices.

[![asciicast](https://asciinema.org/a/t7KBEKP9nSYYJG5v.svg)](https://asciinema.org/a/t7KBEKP9nSYYJG5v)
---

## Table of Contents

- [Supported Environments](#supported-environments)
- [Getting Started — Nginx](#getting-started--nginx-users)
- [Getting Started — Apache](#getting-started--apache-users)
- [Installation](#installation)
- [Usage](#usage)
  - [create](#create--provision-a-new-virtual-host)
  - [remove](#remove--tear-down-a-virtual-host)
  - [disable / enable](#disable--temporarily-deactivate-a-virtual-host)
  - [list / info](#list--show-all-managed-virtual-hosts)
  - [logs](#logs--tail-live-log-files-for-a-virtual-host)
- [Interactive Directory Scaffolding](#interactive-directory-scaffolding)
- [Advanced Runtime Support](#advanced-runtime-support)
  - [PHP-FPM](#php-fpm)
  - [Python (Gunicorn)](#python-gunicorn-proxy)
  - [Node.js (Reverse Proxy)](#nodejs-reverse-proxy)
  - [Local SSL via mkcert](#local-ssl-via-mkcert)
- [Features](#features)
- [Custom Templates](#custom-templates)
- [Automatic Permission & SELinux Management](#automatic-permission--selinux-management)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Development](#development)
- [License](#license)

---

## Supported Environments

VHost Helper v0.3 is validated against the following four configurations:

| Distribution  | Web Server | Config Layout                       | Tested |
|---------------|------------|-------------------------------------|--------|
| Ubuntu/Debian | **Nginx**  | `sites-available` / `sites-enabled` | ✅     |
| Ubuntu/Debian | **Apache** | `sites-available` / `sites-enabled` | ✅     |
| Fedora/RHEL   | **Nginx**  | `conf.d` / `conf.disabled`          | ✅     |
| Fedora/RHEL   | **Apache** | `conf.d` / `conf.disabled`          | ✅     |

---

## Getting Started

### Prerequisites
*   **OS**: Linux (Debian/Ubuntu or Fedora/RHEL)
*   **Python**: 3.10 or higher
*   **Web Server**: Nginx **or** Apache (installed before running `install.sh`)
*   **Permissions**: `sudo` access for privileged operations

---

### Getting Started — Nginx Users

**1. Install Nginx**

```bash
# Debian/Ubuntu
sudo apt-get install -y nginx

# Fedora/RHEL
sudo dnf install -y nginx
```

**2. Install VHost Helper**

```bash
git clone https://github.com/Memleakz/ultimate_vhost.git
cd ultimate_vhost/src
sudo bash install.sh
```

**3. Provision your first site**

```bash
# Auto-detects Nginx (recommended)
vhost create myapp.test /var/www/myapp

# Or explicitly specify the provider
vhost create myapp.test /var/www/myapp --provider nginx
```

**4. Verify**

```bash
vhost list
vhost info myapp.test
```

---

### Getting Started — Apache Users

**1. Install Apache**

```bash
# Debian/Ubuntu
sudo apt-get install -y apache2

# Fedora/RHEL
sudo dnf install -y httpd
```

**2. Install VHost Helper**

```bash
git clone https://github.com/Memleakz/ultimate_vhost.git
cd ultimate_vhost/src
sudo bash install.sh
```

**3. Provision your first site**

```bash
# Explicitly specify Apache as the provider
vhost create myapp.test /var/www/myapp --provider apache
```

**4. Verify**

```bash
vhost list
vhost info myapp.test
```

---

## Installation

### Standard Installation
Clone the repository and run the automated installer:

```bash
git clone https://github.com/Memleakz/ultimate_vhost.git
cd ultimate_vhost/src
sudo bash install.sh
```

The installer:
1. Copies the application to `/opt/vhost-helper/`
2. Creates an isolated Python virtual environment at `/opt/vhost-helper/.venv`
3. Installs all pinned production dependencies
4. Creates a global symlink at `/usr/local/bin/vhost`
5. Installs Bash autocompletion at `/etc/bash_completion.d/vhost`

After installation, `vhost --help` is available from any directory.

### Manual Installation

If you prefer not to use the automated installer:

1. **Clone the repository**: `git clone https://github.com/Memleakz/ultimate_vhost.git`
2. **Create a virtual environment**: `python3 -m venv .venv`
3. **Install dependencies**: `pip install -r requirements.txt`
4. **Create a global symlink**: `sudo ln -s $(pwd)/bin/vhost /usr/local/bin/vhost`
5. **Configure bash completion**: Copy the completion snippet to `/etc/bash_completion.d/vhost`

### Uninstallation

```bash
sudo bash src/uninstall.sh

# Deep clean (removes /opt/vhost-helper and all user config):
sudo bash src/uninstall.sh --deep-clean
```

---

## Usage

### `create` — Provision a New Virtual Host

```bash
# Static site with auto-detected provider
vhost create my-project.test /var/www/my-project

# Force a specific provider
vhost create my-project.test /var/www/my-project --provider nginx
vhost create my-project.test /var/www/my-project --provider apache

# PHP-FPM — auto-detect highest installed version
vhost create php-app.test /var/www/php-app --php
vhost create php-app.test /var/www/php-app --php --provider apache

# PHP-FPM — require a specific version (exits with code 1 if not found)
vhost create php-app.test /var/www/php-app --php 8.2
vhost create php-app.test /var/www/php-app --php 8.1 --provider nginx

# Python (Gunicorn) reverse proxy
vhost create api.test /var/www/api --python --python-port 8000 --provider nginx

# Node.js reverse proxy (default port 3000)
vhost create node-app.test /var/www/node-app --nodejs --provider nginx

# Node.js on a custom port
vhost create node-app.test /var/www/node-app --nodejs --node-port 8080

# Node.js via Unix Domain Socket
vhost create node-app.test /var/www/node-app --nodejs --node-socket /run/node-app/app.sock

# Custom listen port
vhost create staging.test /var/www/staging --port 8080

# HTTPS with a locally-trusted certificate (requires mkcert)
vhost create myapp.test /var/www/myapp --mkcert

# HTTPS with a custom certificate storage directory
vhost create myapp.test /var/www/myapp --mkcert --ssl-dir /home/user/.certs

# Non-interactive: create the document root automatically and scaffold index.html
vhost create myapp.test /var/www/myapp --create-dir --scaffold

# Non-interactive: abort if the document root does not exist
vhost create myapp.test /var/www/myapp --no-create-dir
```

### `remove` — Tear Down a Virtual Host

```bash
# Interactive confirmation
vhost remove my-project.test

# Skip confirmation
vhost remove my-project.test --force

# Explicit provider
vhost remove my-project.test --provider nginx --force
```

### `disable` — Temporarily Deactivate a Virtual Host

Deactivates the site without deleting its configuration. The `/etc/hosts` entry is also removed.

```bash
vhost disable my-project.test

# Explicit provider
vhost disable my-project.test --provider apache
```

On Debian/Ubuntu: removes the symlink from `sites-enabled`.
On Fedora/RHEL: moves the config file to `conf.disabled`.

### `enable` — Re-Activate a Disabled Virtual Host

```bash
vhost enable my-project.test

# Explicit provider
vhost enable my-project.test --provider apache
```

Restores the site configuration and re-adds the `/etc/hosts` entry.

### `list` — Show All Managed Virtual Hosts

```bash
vhost list
```

Displays a Rich table of all vhosts found across both Nginx and Apache configuration directories.

### `info` — Show Details for a Specific Domain

```bash
# Domain info (shows config, detected provider, status)
vhost info my-project.test

# System info (shows detected OS, family, installed providers)
vhost info
```

### `logs` — Tail Live Log Files for a Virtual Host

Stream server logs directly to your terminal. The command locates the correct log file paths by reading the active configuration file — no manual path hunting required.

```bash
# Tail both access and error logs (default)
vhost logs myapp.test

# Tail only the error log
vhost logs myapp.test --error

# Tail only the access log
vhost logs myapp.test --access

# Explicit provider override
vhost logs myapp.test --provider apache
vhost logs myapp.test --provider nginx
```

**Behaviour:**
- Reads `access_log` / `error_log` (Nginx) or `CustomLog` / `ErrorLog` (Apache) directives directly from the active config file.
- Exits with a descriptive error if:
  - The vhost is not enabled (config not found in `sites-enabled` / `conf.d`).
  - No log directive is present in the configuration.
  - The log file does not exist on the filesystem.
- `--error` and `--access` are mutually exclusive. Use one at a time.
- Press `Ctrl+C` to stop tailing; exits with code `0`.

---

## Interactive Directory Scaffolding

When `vhost create` is run with a non-existent `document_root`, VHost Helper automatically handles the setup so you can reach a working "It works!" page in a single command.

### How It Works

**Step 1 — Directory check**

If the target directory does not exist, the tool checks the environment:

| Environment | Behaviour |
|---|---|
| Interactive TTY, no flag | Prompts: `Directory '/path' does not exist. Create it? [Y/n]` |
| `--create-dir` flag | Creates the directory without prompting |
| `--no-create-dir` flag | Prints an error and exits with code `1` |
| Non-interactive (no TTY) | Creates the directory automatically (safe pipeline default) |

**Step 2 — index.html generation**

If the directory was just created (or is empty), the tool offers to scaffold a starter page:

| Environment | Behaviour |
|---|---|
| Interactive TTY, no flag | Prompts: `Generate an index.html so you can test it immediately? [Y/n]` |
| `--scaffold` flag | Generates `index.html` without prompting |
| `--no-scaffold` flag | Skips generation entirely |
| Non-interactive (no TTY) | Skips generation (safe default — do not overwrite CI artifacts) |

The generated `index.html` is a professional "It Works! Powered by ultimate_vhost" landing page rendered from `templates/common/index.html.j2`. Ownership and permissions (`644`, `<user>:<webserver-group>`) match the rest of the webroot.

### Scaffolding Flags

| Flag | Description |
|---|---|
| `--create-dir` | Create the document root directory if it does not exist (no prompt). |
| `--no-create-dir` | Abort with exit code `1` if the document root does not exist. |
| `--scaffold` | Generate a starter `index.html` in an empty document root (no prompt). |
| `--no-scaffold` | Never generate an `index.html`, even after creating the document root. |

`--create-dir` and `--no-create-dir` are mutually exclusive. `--scaffold` and `--no-scaffold` are mutually exclusive.

### Examples

```bash
# Fully interactive — prompts for both directory and index.html
vhost create myapp.test /var/www/myapp

# CI/script: create everything automatically
vhost create myapp.test /var/www/myapp --create-dir --scaffold

# Fail fast if the webroot is not already prepared
vhost create myapp.test /var/www/myapp --no-create-dir

# Create the directory but skip the index.html (you will add your own files)
vhost create myapp.test /var/www/myapp --create-dir --no-scaffold
```

---

## Advanced Runtime Support

### PHP-FPM

The `--php` flag automatically discovers and configures the correct PHP-FPM socket for your distribution. Pass an optional version argument (`--php 8.2`) when you need a specific PHP version.

#### Auto-detect (recommended)

```bash
# Detect the highest installed PHP-FPM version automatically
vhost create php-app.test /var/www/php-app --php
vhost create php-app.test /var/www/php-app --php --provider apache
```

#### Explicit version

```bash
# Target a specific PHP version — exits with code 1 if that version is not installed
vhost create php-app.test /var/www/php-app --php 8.2
vhost create php-app.test /var/www/php-app --php 8.1 --provider nginx
```

#### Socket path resolution

| Distribution  | Socket Path (per version)                         |
|---------------|---------------------------------------------------|
| Debian/Ubuntu | `/run/php/php<VERSION>-fpm.sock`                  |
| Fedora/RHEL   | `/run/php-fpm/www.sock` (version-agnostic)        |

After creating the vhost, VHost Helper attempts `systemctl enable --now php<VERSION>-fpm` automatically. If the service fails to start, a **non-blocking warning** is printed and vhost creation still succeeds.

### Python (Gunicorn Proxy)

```bash
# Assumes Gunicorn is running on localhost:8000
vhost create api.test /var/www/api --python --python-port 8000 --provider nginx
```

### Node.js (Reverse Proxy)

VHost Helper configures the web server as a reverse proxy in front of a running Node.js process. Both TCP port and Unix Domain Socket (UDS) upstreams are supported.

```bash
# Default: proxies to http://localhost:3000
vhost create node-app.test /var/www/node-app --nodejs

# Custom port
vhost create node-app.test /var/www/node-app --nodejs --node-port 8080

# Unix Domain Socket (highest performance)
vhost create node-app.test /var/www/node-app --nodejs --node-socket /run/node-app/app.sock

# Explicit runtime flag (equivalent to --nodejs)
vhost create node-app.test /var/www/node-app --runtime nodejs --node-port 4000
```

The Nginx template adds WebSocket-upgrade headers (`Connection`, `Upgrade`) automatically. The Apache template activates `mod_proxy` and `mod_proxy_http` comment notices and uses `ProxyPreserveHost On`.

---

### Local SSL via mkcert

The `--mkcert` flag provisions a locally-trusted certificate and key pair for the domain and configures the web server to listen on port 443. The HTTP virtual host automatically redirects to HTTPS.

**Prerequisites**: [`mkcert`](https://github.com/FiloSottile/mkcert) must be installed and `mkcert -install` must have been run once on the machine to register the local CA in the system trust store.

```bash
# Install mkcert (one-time)
# Debian/Ubuntu
sudo apt install mkcert && mkcert -install

# Fedora/RHEL
sudo dnf install mkcert && mkcert -install
```

```bash
# Create an HTTPS vhost (certificate stored in /etc/vhost-helper/ssl/)
sudo vhost create myapp.test /var/www/myapp --mkcert

# Specify a custom certificate directory
sudo vhost create myapp.test /var/www/myapp --mkcert --ssl-dir /opt/certs

# HTTPS + Apache
sudo vhost create myapp.test /var/www/myapp --mkcert --provider apache
```

After creation, navigate to `https://myapp.test` — the browser shows a green padlock because the certificate is signed by the mkcert local CA.

**Certificate file layout** (default directory `/etc/vhost-helper/ssl/`):

```
/etc/vhost-helper/ssl/
├── myapp.test.pem       (certificate, mode 0640)
└── myapp.test-key.pem   (private key, mode 0640)
```

**SSL directory precedence** (highest to lowest):

| Source | Example |
|--------|---------|
| `--ssl-dir` CLI flag | `--ssl-dir /opt/certs` |
| `VHOST_SSL_DIR` environment variable | `export VHOST_SSL_DIR=/opt/certs` |
| Built-in default | `/etc/vhost-helper/ssl` |

> If `--mkcert` is omitted, the tool generates a standard HTTP (port 80) configuration with zero changes to SSL behaviour.

---

## Features

*   **Interactive Directory Scaffolding**: When the document root is missing, `vhost create` offers to create it and generate a "It Works!" `index.html` with correct ownership — getting you to a working browser page in a single command. Fully controllable via `--create-dir`, `--no-create-dir`, `--scaffold`, and `--no-scaffold` flags for CI environments.
*   **Atomic Hostfile Management**: Safely add or remove entries in `/etc/hosts` without corrupting existing mappings. Duplicate detection prevents double entries.
*   **Automated Permission Management**: Automatically configures web server group ownership (`www-data`, `nginx`, `apache`, etc.), directory modes (`755`), file modes (`644`), and the SetGID bit — so you never hit a `403 Forbidden` again. SELinux context hardening (`httpd_sys_content_t`) is applied automatically on RHEL/Fedora.
*   **Config Syntax Validation Before Reload**: Every `vhost create` and `vhost enable` runs `nginx -t` (Nginx) or `httpd -t` / `apache2ctl configtest` (Apache) before touching the live service. If the generated config is invalid, it is automatically rolled back — your web server is never left in a broken state.
*   **Intelligent OS Detection**: Distribution-agnostic support for **Debian/Ubuntu** and **RHEL/CentOS/Fedora**.
*   **Multi-Provider Architecture**: Native support for **Nginx** and **Apache** with intelligent auto-detection via binary and config-path scanning.
*   **Multi-Runtime Support**: One-command provisioning for **Static HTML**, **PHP-FPM**, **Python (Gunicorn)**, and **Node.js (Reverse Proxy)** applications.
*   **Smart PHP-FPM Auto-Detection**: `--php` discovers the highest installed PHP-FPM version automatically. `--php X.Y` targets a specific version and exits immediately with a descriptive error if that version is not present on the system. The correct PHP-FPM service (`php<VERSION>-fpm` on Debian, `php-fpm` on RHEL) is started automatically after vhost creation — a non-blocking warning is shown if the service fails to start.
*   **Local SSL via mkcert**: The `--mkcert` flag automatically generates a locally-trusted certificate, configures port-443 listeners, and adds an HTTP→HTTPS redirect — all in a single command.
*   **Security First**: Operations are performed via targeted `sudo` escalation rather than running the entire tool as root. Path injection validation is enforced on all domain inputs.
*   **Hierarchical Template Engine**: Custom templates in `~/.config/vhost_helper/templates/` take precedence over bundled defaults.
*   **Developer Experience**: Includes automated Bash autocompletion and a professional CLI interface powered by `Typer` and `Rich`.
*   **Clean Uninstallation**: A dedicated uninstaller with a `--deep-clean` option ensures your system remains tidy after use.

---

## Custom Templates

VHost Helper supports a two-level template hierarchy:

| Priority     | Location                                                          |
|--------------|-------------------------------------------------------------------|
| 1 (highest)  | `~/.config/vhost_helper/templates/<provider>/<name>.conf.j2`     |
| 2 (fallback) | `/opt/vhost-helper/templates/<provider>/<name>.conf.j2`          |

The user directory is created automatically on first run. Drop a file named `default.conf.j2` (or any custom name referenced via `--template`) into the appropriate provider directory to override the built-in template.

### Template Variable Reference

The following Jinja2 variables are injected at render time and are available in every `.conf.j2` file:

| Variable       | Type  | Default                   | Providers      | Description |
|----------------|-------|---------------------------|----------------|-------------|
| `domain`       | `str` | *(required)*              | nginx, apache  | Primary domain name (e.g. `example.com`) |
| `document_root`| `str` | *(required)*              | nginx, apache  | Absolute path to the web root directory |
| `port`         | `int` | `80`                      | nginx, apache  | TCP port the virtual host listens on |
| `runtime`      | `str` | `static`                  | nginx, apache  | Runtime mode — see values below |
| `python_port`  | `int` | `8000`                    | nginx, apache  | gunicorn/uvicorn port (only when `runtime=python`) |
| `php_socket`   | `str\|None` | `None`             | nginx, apache  | PHP-FPM unix socket path. Set automatically by `--php` / `--php 8.2`. When non-`None`, PHP `location` / `FilesMatch` blocks are rendered in the template. |
| `node_port`    | `int` | `3000`                    | nginx, apache  | Node.js upstream TCP port (only when `runtime=nodejs`, ignored when `node_socket` is set) |
| `node_socket`  | `str\|None` | `None`             | nginx, apache  | Unix Domain Socket path for Node.js (only when `runtime=nodejs`; overrides `node_port` when set) |
| `os_family`    | `str` | *(auto-detected)*         | nginx, apache  | `debian_family` or `rhel_family` — controls provider-specific config paths |

**`runtime` values**

| Value      | Behaviour |
|------------|-----------|
| `static`   | Serves static files with `try_files` / `index.html` fallback |
| `php`      | FastCGI pass to `{{ php_socket }}` |
| `python`   | HTTP reverse proxy to `http://127.0.0.1:{{ python_port }}` |
| `nodejs`   | HTTP reverse proxy to `http://127.0.0.1:{{ node_port }}` (or Unix socket when `node_socket` is set) |

> **Tip:** Run `vhost template-vars` to view this reference directly in your terminal.

### Minimal Custom Template Example

```nginx
{# my-custom.conf.j2 — place in ~/.config/vhost_helper/templates/nginx/ #}
server {
    listen {{ port }};
    server_name {{ domain }};
    root "{{ document_root }}";

    {% if runtime == 'python' %}
    location / {
        proxy_pass http://127.0.0.1:{{ python_port }};
    }
    {% else %}
    location / { try_files $uri $uri/ /index.html; }
    {% endif %}
}
```

Use it with: `vhost create example.com /var/www/example --template my-custom`

---

## Configuration

VHost Helper reads system paths at startup. All path overrides are only active when `VHOST_TEST_MODE=1` — this prevents privilege-escalation attacks when the tool runs under `sudo`.

| Variable | Description | Default |
|---|---|---|
| `VHOST_TEST_MODE` | Set to `1` to enable all path overrides (dev/test only). | `0` |
| `NGINX_SITES_AVAILABLE` | Path to Nginx `sites-available` (or `conf.d` on RHEL). | `/etc/nginx/sites-available` |
| `NGINX_SITES_ENABLED` | Path to Nginx `sites-enabled`. | `/etc/nginx/sites-enabled` |
| `NGINX_SITES_DISABLED` | Path to Nginx disabled configs (RHEL only). | `/etc/nginx/conf.disabled` |
| `APACHE_SITES_AVAILABLE` | Path to Apache `sites-available` (or `conf.d` on RHEL). | `/etc/apache2/sites-available` |
| `APACHE_SITES_ENABLED` | Path to Apache `sites-enabled` (Debian only). | `/etc/apache2/sites-enabled` |
| `APACHE_SITES_DISABLED` | Path to Apache disabled configs (RHEL only). | `/etc/httpd/conf.disabled` |
| `VHOST_HOSTS_FILE` | Override path to the system hosts file. | `/etc/hosts` |
| `VHOST_SSL_DIR` | Override the default SSL certificate storage directory. | `/etc/vhost-helper/ssl` |

```bash
# Copy the template and uncomment variables you need
cp .env.example .env
```

---

## Development

### Setup Dev Environment

```bash
git clone https://github.com/Memleakz/ultimate_vhost.git
cd ultimate_vhost/src
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

### Running Tests

The project maintains a rigorous test suite with over **1087 tests** and a mandatory **80% coverage threshold** (current: 99%).

```bash
# Run full suite with coverage
PYTHONPATH=lib pytest --cov=lib/vhost_helper --cov-report=term-missing tests/
```

### Running Integration Tests

```bash
# Tests all four matrix configurations
bash scripts/run_integration_tests.sh
```

---

## Architecture

VHost Helper uses a modular, provider-based architecture. `config.py` handles distribution-agnostic path resolution, while specific logic is encapsulated in `NginxProvider` and `ApacheProvider`.

| Layer       | Module          | Responsibility                        |
|-------------|-----------------|---------------------------------------|
| CLI         | `main.py`       | Typer commands, provider auto-detection |
| Providers   | `providers/`    | Nginx and Apache specific logic        |
| PHP-FPM     | `php_fpm.py`    | Version discovery, socket resolution, service orchestration |
| OS Detection| `os_detector.py`| `/etc/os-release` parsing             |
| Hostfile    | `hostfile.py`   | Atomic `/etc/hosts` management        |
| Config      | `config.py`     | Distribution-aware path constants     |
| Scaffolding | `scaffolding.py`| Interactive directory creation and `index.html` generation |

### Provider Auto-Detection

When `--provider` is not specified, the CLI uses this priority chain:

1. Search both Nginx and Apache config directories for an existing `.conf` file matching the domain.
2. If exactly one provider has a matching config, use that provider.
3. If no config exists yet (e.g., `create`), check which server binaries are installed (`nginx`, `apache2`, `httpd`).
4. If only one binary is found, use that provider.
5. If both or neither are found, default to Nginx.

## Automatic Permission & SELinux Management

After a virtual host is created, VHost Helper automatically applies the "gold standard" permission model to the webroot directory, eliminating the most common cause of post-creation "403 Forbidden" errors.

### Default Behaviour

When `vhost create` completes successfully, the following four operations are applied to the `<document_root>`:

1. **Ownership** — `chown -R <current_user>:<webserver_group> <document_root>`  
   The owner is set to the currently logged-in user. The group is resolved from the distribution × provider matrix:

   | Distribution  | Provider | Default Group |
   |---------------|----------|---------------|
   | Debian/Ubuntu | Nginx    | `www-data`    |
   | Debian/Ubuntu | Apache   | `www-data`    |
   | Fedora/RHEL   | Nginx    | `nginx`       |
   | Fedora/RHEL   | Apache   | `apache`      |

2. **Directory permissions** — `find <document_root> -type d -exec chmod 755 {} +`  
   Result: `drwxr-xr-x` on all directories.

3. **File permissions** — `find <document_root> -type f -exec chmod 644 {} +`  
   Result: `-rw-r--r--` on all files.

4. **SetGID bit** — `find <document_root> -type d -exec chmod g+s {} +`  
   Result: `drwxr-sr-x`. New files created inside the webroot inherit the web server group automatically.

### SELinux Context Hardening (RHEL/Fedora Only)

On RHEL/Fedora hosts where SELinux is in `Enforcing` or `Permissive` mode, VHost Helper also applies the `httpd_sys_content_t` security context to the webroot:

- **Preferred (persistent)**: `semanage fcontext` + `restorecon` — the label survives a `restorecon -R` sweep.
- **Fallback (non-persistent)**: `chcon -Rt httpd_sys_content_t <document_root>` — used when `semanage` is not installed.

Verify with: `ls -Z <document_root>` — the output should show `httpd_sys_content_t`.

### Override Flags

All four aspects of the permission model can be overridden on the `vhost create` command:

| Flag | Default | Description |
|------|---------|-------------|
| `--webroot-user TEXT` | Current login user | Override the owner applied by `chown`. |
| `--webroot-group TEXT` | Resolved service group | Override the group applied by `chown`. |
| `--webroot-perms DIR:FILE` | `755:644` | Override directory and file octal modes (e.g. `750:640`). |
| `--skip-permissions` | `false` | Skip all permission and SELinux steps entirely. |

`--skip-permissions` is mutually exclusive with the other three flags.

### Examples

```bash
# Default: current user as owner, 'www-data' as group (Debian+Nginx)
vhost create mysite.test /var/www/mysite

# Custom group for an Apache deployment on RHEL
vhost create mysite.test /var/www/mysite --provider apache --webroot-group apache

# Tighter permissions for a production site
vhost create mysite.test /var/www/mysite --webroot-perms 750:640

# Skip all permission management (you handle your own ACLs)
vhost create mysite.test /var/www/mysite --skip-permissions

# Full override: custom owner, custom group, custom modes
vhost create mysite.test /var/www/mysite \
  --webroot-user deploy \
  --webroot-group www-data \
  --webroot-perms 755:644
```

---

## License

MIT License
