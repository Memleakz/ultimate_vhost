# VHost Helper — AI Skill Definition

> **Version**: v0.1 | **Ticket**: ULTIMATE_VHOST-019  
> **Purpose**: Machine-readable CLI skill contract for AI agents (Claude, GitHub Copilot, Gemini CLI, OpenCode).  
> This file is the authoritative reference for autonomous vhost provisioning. An agent given only this file as context must be able to generate valid `vhost` commands without further coaching.

---

## Overview

`vhost` is a unified CLI tool for managing Nginx and Apache virtual hosts on Debian/Ubuntu and Fedora/RHEL systems. It automates `/etc/hosts` management, config generation, service reload, and atomic rollback.

**Binary**: `vhost` (installed globally at `/usr/local/bin/vhost` by `install.sh`)  
**Privilege model**: Most write/reload commands require `sudo` or root — see [Safety Protocol](#safety-protocol).

---

## Provider & Auto-Detection

All commands that modify system state accept an optional `--provider` flag. When it is **not** supplied, the tool runs the following four-step cascade to select the correct provider automatically:

```
Step 1 — Config-file scan
  Check known config directories for an existing <domain>.conf file.
  • Nginx paths: NGINX_SITES_AVAILABLE, NGINX_SITES_ENABLED, NGINX_SITES_DISABLED
  • Apache paths: APACHE_SITES_AVAILABLE, APACHE_SITES_ENABLED, APACHE_SITES_DISABLED
  → If found in Nginx paths only  → use NginxProvider
  → If found in Apache paths only → use ApacheProvider

Step 2 — Binary detection (shutil.which)
  • nginx   present → NginxProvider
  • apache2 present → ApacheProvider (Debian/Ubuntu)
  • httpd   present → ApacheProvider (Fedora/RHEL)
  → Only one binary found → use that provider

Step 3 — Config-directory existence
  • Only Nginx config dirs exist  → NginxProvider
  • Only Apache config dirs exist → ApacheProvider

Step 4 — Default fallback
  → NginxProvider
```

The `--provider` flag **always overrides** this cascade and short-circuits all four steps.

Valid values: `nginx`, `apache`

---

## Distribution × Provider Path Table

Agents must use this table to anticipate where config files will be written **before** running the tool.

| Distribution  | Web Server | Config directory               | Active/enabled location        | Disable mechanism                  |
|---------------|------------|--------------------------------|--------------------------------|------------------------------------|
| Debian/Ubuntu | Nginx      | `/etc/nginx/sites-available`   | `/etc/nginx/sites-enabled`     | Remove symlink                     |
| Debian/Ubuntu | Apache     | `/etc/apache2/sites-available` | `/etc/apache2/sites-enabled`   | Remove symlink                     |
| Fedora/RHEL   | Nginx      | `/etc/nginx/conf.d`            | `/etc/nginx/conf.d`            | Move to `/etc/nginx/conf.disabled` |
| Fedora/RHEL   | Apache     | `/etc/httpd/conf.d`            | `/etc/httpd/conf.d`            | Move to `/etc/httpd/conf.disabled` |

All four path constants are overridable via environment variables for testing without root:

| Environment Variable       | Default path                   |
|----------------------------|--------------------------------|
| `NGINX_SITES_AVAILABLE`    | `/etc/nginx/sites-available`   |
| `NGINX_SITES_ENABLED`      | `/etc/nginx/sites-enabled`     |
| `APACHE_SITES_AVAILABLE`   | `/etc/apache2/sites-available` |
| `APACHE_SITES_ENABLED`     | `/etc/apache2/sites-enabled`   |

---

## Safety Protocol

> **All agents must read and apply this section before generating any `vhost` command.**

1. **Privilege requirement**: The `create`, `remove`, `enable`, and `disable` commands write to system directories (`/etc/hosts`, `/etc/nginx/`, `/etc/apache2/`, `/etc/httpd/`) and reload system services. They **must** be prefixed with `sudo` when the invoking user is not root.

   ```sh
   sudo vhost create mysite.local /var/www/mysite
   sudo vhost remove mysite.local --force
   ```

2. **`remove` is irreversible**: The `remove` command permanently deletes configuration files and the `/etc/hosts` entry. Agents **must not** invoke `remove` without explicit user confirmation. Use `--force` only when the user has explicitly consented to deletion. Prefer `disable` as the non-destructive alternative.

3. **`disable` is the safe alternative**: `disable` deactivates a vhost without deleting any files. It is idempotent — calling it on an already-disabled vhost is a no-op.

4. **No shell injection risk**: The tool never uses `shell=True`. All elevated commands are passed as argument lists, eliminating shell-injection vectors. Agents must not wrap `vhost` in `sh -c "..."` constructs.

5. **Atomic rollback**: If `create` fails after updating `/etc/hosts`, the tool automatically rolls back the hostfile entry. Agents do not need to perform manual cleanup on failure.

---

## Command: create

**Purpose**: Provision a new virtual host — writes `/etc/hosts`, generates a web-server config, validates it, and reloads the service.

```yaml
# Full parameter specification
command: vhost create
arguments:
  domain:
    type: string
    required: true
    description: "DNS-valid domain name (e.g. mysite.local, app.example.test)"
    constraints: "RFC 1035 labels; no leading/trailing hyphens; max 253 chars total"
  document_root:
    type: path (absolute)
    required: true
    description: "Absolute filesystem path to the site root. Must already exist."
options:
  --provider, -p:
    type: enum [nginx, apache]
    required: false
    default: auto-detected
    description: "Force a specific web-server provider."
  --port:
    type: integer (1–65535)
    required: false
    default: 80
    description: "HTTP port the server block listens on."
  --php:
    type: flag
    required: false
    default: false
    description: "Enable PHP-FPM reverse proxy. Mutually exclusive with --python, --nodejs, --runtime."
  --python:
    type: flag
    required: false
    default: false
    description: "Enable Gunicorn reverse proxy. Mutually exclusive with --php, --nodejs, --runtime."
  --python-port:
    type: integer
    required: false
    default: 8000
    description: "Gunicorn upstream port. Only used when --python is set."
  --nodejs:
    type: flag
    required: false
    default: false
    description: "Enable Node.js reverse proxy. Mutually exclusive with --php, --python, --runtime."
  --node-port:
    type: integer
    required: false
    default: 3000
    description: "Node.js upstream port. Used with --nodejs or --runtime nodejs. Ignored when --node-socket is set."
  --node-socket:
    type: string (path)
    required: false
    default: null
    description: "Unix Domain Socket path for Node.js upstream. Overrides --node-port when set."
  --runtime:
    type: enum [static, php, python, nodejs]
    required: false
    default: static
    description: "Explicit runtime mode. Mutually exclusive with --php, --python, --nodejs."
  --template, -t:
    type: string
    required: false
    default: "default"
    description: "Jinja2 template name to use. Defaults to the mode-appropriate built-in template."
exit_codes:
  0: "Success — vhost created and service reloaded (or skip noted if service not running)."
  1: "Error — domain invalid, document_root missing, provider not installed, or config failed."
mutual_exclusion: "--php, --python, --nodejs, and --runtime cannot be combined."
```

### Examples

```sh
# Static site (default runtime) — Nginx auto-detected
sudo vhost create mysite.local /var/www/mysite

# Static site — Apache provider forced
sudo vhost create mysite.local /var/www/mysite --provider apache

# PHP-FPM site (shorthand flag)
sudo vhost create app.local /var/www/app --php

# PHP-FPM site on Apache (explicit provider)
sudo vhost create app.local /var/www/app --php --provider apache

# Python/Gunicorn proxy on default port 8000
sudo vhost create api.local /var/www/api --python

# Python/Gunicorn proxy on custom port 5000
sudo vhost create api.local /var/www/api --python --python-port 5000

# Node.js proxy on default port 3000
sudo vhost create node.local /var/www/node --runtime nodejs

# Node.js proxy on custom port 8080 (agent use-case: explicit port)
sudo vhost create node.local /var/www/node --runtime nodejs --node-port 8080

# Node.js proxy via Unix Domain Socket
sudo vhost create node.local /var/www/node --nodejs --node-socket /run/node-app/app.sock

# Custom port 8080 with PHP runtime
sudo vhost create shop.local /var/www/shop --php --port 8080
```

---

## Command: remove

**Purpose**: Tear down an existing virtual host — removes web-server config files and the `/etc/hosts` entry; reloads the service.

```yaml
command: vhost remove
arguments:
  domain:
    type: string
    required: true
    description: "Domain name of the vhost to remove."
options:
  --provider, -p:
    type: enum [nginx, apache]
    required: false
    default: auto-detected
    description: "Force a specific provider; uses auto-detection when omitted."
  --force, -f:
    type: flag
    required: false
    default: false
    description: "Skip the interactive confirmation prompt. Use only with explicit user consent."
exit_codes:
  0: "Success — vhost removed and service reloaded."
  1: "Error — domain not found, or removal failed."
destructive: true
```

> ⚠ **Agent warning**: `remove` is permanent. Do not use `--force` unless the user has explicitly confirmed they want to delete the vhost. Prefer `disable` to temporarily deactivate.

### Examples

```sh
# Interactive removal — prompts for confirmation
sudo vhost remove mysite.local

# Non-interactive removal (user has confirmed intent)
sudo vhost remove mysite.local --force

# Remove an Apache-managed vhost explicitly
sudo vhost remove mysite.local --provider apache --force
```

---

## Command: list

**Purpose**: Display all managed virtual hosts across both providers in a formatted table, showing domain, document root, server type, and enabled/disabled status.

```yaml
command: vhost list
arguments: []
options: []
exit_codes:
  0: "Success — table printed (may be empty if no vhosts exist)."
notes: "Reads from both NGINX_SITES_AVAILABLE and APACHE_SITES_AVAILABLE. No sudo required."
```

### Examples

```sh
# List all vhosts
vhost list
```

---

## Command: info

**Purpose**: Display detailed configuration metadata for a specific domain, or show system-level server detection information when no domain is supplied.

```yaml
command: vhost info
arguments:
  domain:
    type: string
    required: false
    description: "Domain name to inspect. Omit to display system/server detection info."
options:
  --provider, -p:
    type: enum [nginx, apache]
    required: false
    default: auto-detected
    description: "Force provider when the domain exists under both servers."
exit_codes:
  0: "Success — info panel displayed."
  1: "Error — domain specified but no config file found."
```

### Examples

```sh
# Show config details for a domain
vhost info mysite.local

# Show info for an Apache-managed vhost
vhost info mysite.local --provider apache

# Show system-level info (OS, installed servers) — no sudo required
vhost info
```

---

## Command: enable

**Purpose**: Activate a previously disabled virtual host — restores the symlink (Debian) or moves the config file back (RHEL), then reloads the service. Idempotent.

```yaml
command: vhost enable
arguments:
  domain:
    type: string
    required: true
    description: "Domain name of the vhost to enable."
options:
  --provider, -p:
    type: enum [nginx, apache]
    required: false
    default: auto-detected
    description: "Force provider; uses auto-detection when omitted."
exit_codes:
  0: "Success — vhost enabled (or no-op if already enabled)."
  1: "Error — domain config not found."
idempotent: true
```

### Examples

```sh
# Enable a vhost (provider auto-detected)
sudo vhost enable mysite.local

# Enable an Apache vhost explicitly
sudo vhost enable mysite.local --provider apache
```

---

## Command: disable

**Purpose**: Deactivate a virtual host without deleting its configuration files — removes the symlink (Debian) or moves the config to `conf.disabled/` (RHEL), then reloads the service. Idempotent. **Preferred over `remove` when the intent is temporary deactivation.**

```yaml
command: vhost disable
arguments:
  domain:
    type: string
    required: true
    description: "Domain name of the vhost to disable."
options:
  --provider, -p:
    type: enum [nginx, apache]
    required: false
    default: auto-detected
    description: "Force provider; uses auto-detection when omitted."
exit_codes:
  0: "Success — vhost disabled (or no-op if already disabled)."
  1: "Error — domain config not found."
idempotent: true
destructive: false
```

### Examples

```sh
# Disable a vhost (provider auto-detected)
sudo vhost disable mysite.local

# Disable an Nginx vhost explicitly
sudo vhost disable mysite.local --provider nginx
```

---

## Runtime Mode Reference

| Flag / `--runtime` value | Use case | Key options |
|--------------------------|----------|-------------|
| *(none)* / `static`      | Static file serving | — |
| `--php` / `php`          | PHP-FPM via Unix socket | Distribution-aware socket path auto-resolved |
| `--python` / `python`    | Gunicorn upstream | `--python-port` (default `8000`) |
| `--nodejs` / `nodejs`    | Node.js upstream | `--node-port` (default `3000`), `--node-socket` (UDS, overrides port) |

Flags `--php`, `--python`, `--nodejs`, and `--runtime` are **mutually exclusive**. Combining any two produces exit code 1.

---

## PHP Socket Paths (Distribution-Aware)

The `--php` flag automatically resolves the correct PHP-FPM socket for the running distribution:

| Distribution  | PHP-FPM socket path                        |
|---------------|--------------------------------------------|
| Debian/Ubuntu | `/run/php/php-fpm.sock` (version-agnostic) |
| Fedora/RHEL   | `/run/php-fpm/www.sock`                    |

No manual `--php-socket` flag is required; the tool detects the OS at runtime.

---

## Agent Quick-Reference: Common Scenarios

| Goal | Command |
|------|---------|
| Create a PHP vhost on Apache (Debian) | `sudo vhost create app.local /var/www/app --php --provider apache` |
| Create a Node.js vhost on port 8080 | `sudo vhost create node.local /var/www/node --runtime nodejs --node-port 8080` |
| Create a Node.js vhost via Unix socket | `sudo vhost create node.local /var/www/node --nodejs --node-socket /run/node-app/app.sock` |
| Temporarily disable a vhost | `sudo vhost disable mysite.local` |
| Re-enable a disabled vhost | `sudo vhost enable mysite.local` |
| Permanently delete a vhost | `sudo vhost remove mysite.local --force` |
| Check what vhosts exist | `vhost list` |
| Inspect a specific vhost | `vhost info mysite.local` |
| Check installed servers (no domain) | `vhost info` |
