from pydantic import BaseModel, Field, field_validator, model_validator
from pathlib import Path
from enum import Enum
from typing import Optional


class ServerType(str, Enum):
    NGINX = "nginx"
    APACHE = "apache"


class RuntimeMode(str, Enum):
    STATIC = "static"
    PHP = "php"
    PYTHON = "python"
    NODEJS = "nodejs"


# Default php-fpm socket paths keyed by OS family.
# Contains both short forms (for unit tests) and canonical forms with the _family suffix.
PHP_SOCKET_PATHS: dict[str, str] = {
    "debian": "/run/php/php-fpm.sock",
    "rhel": "/run/php-fpm/www.sock",
    "arch": "/run/php-fpm/php-fpm.sock",
    "debian_family": "/run/php/php-fpm.sock",
    "rhel_family": "/run/php-fpm/www.sock",
    "arch_family": "/run/php-fpm/php-fpm.sock",
}
DEFAULT_PHP_SOCKET = "/run/php/php-fpm.sock"


class VHostConfig(BaseModel):
    domain: str = Field(
        ...,
        description="The domain name (e.g., mysite.test)",
        pattern=r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$",
    )
    document_root: Path = Field(
        ..., description="The absolute path to the project root"
    )
    server_type: ServerType = Field(
        default=ServerType.NGINX, description="The web server type"
    )
    enabled: bool = Field(
        default=True, description="Current status of the virtual host"
    )
    port: int = Field(default=80, description="Port number", ge=1, le=65535)
    runtime: RuntimeMode = Field(
        default=RuntimeMode.STATIC, description="Language runtime mode"
    )
    python_port: int = Field(
        default=8000,
        description="Gunicorn upstream port (Python runtime)",
        ge=1,
        le=65535,
    )
    node_port: int = Field(
        default=3000,
        description="Node.js upstream port (nodejs runtime)",
        ge=1,
        le=65535,
    )
    node_socket: Optional[str] = Field(
        default=None,
        description="Unix Domain Socket path for Node.js (overrides node_port when set)",
    )
    php_socket: Optional[str] = Field(
        default=None,
        description="PHP-FPM Unix socket path. When set must be an absolute path (starts with '/').",
    )
    template: str = Field(
        default="default", description="The template used to create the config"
    )
    ssl_enabled: bool = Field(
        default=False, description="Whether SSL (HTTPS) is enabled via mkcert"
    )
    cert_path: Optional[Path] = Field(
        default=None, description="Absolute path to the SSL certificate file (.pem)"
    )
    key_path: Optional[Path] = Field(
        default=None, description="Absolute path to the SSL private key file (-key.pem)"
    )

    @field_validator("document_root")
    @classmethod
    def validate_document_root(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(f"Document root {v} does not exist.")
        if not v.is_dir():
            raise ValueError(f"Document root {v} must be a directory.")

        # Security: Prevent configuration injection in Nginx/Apache templates.
        # Check for characters that could break out of a double-quoted string.
        forbidden = ['"', "\n", "\r"]
        if any(char in str(v) for char in forbidden):
            raise ValueError(
                "Document root path contains forbidden characters (quotes or newlines)."
            )

        return v

    @field_validator("node_socket")
    @classmethod
    def validate_node_socket(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v != "":
            if not v.startswith("/"):
                raise ValueError(
                    f"node_socket must be an absolute path (must start with '/'), got: '{v}'"
                )
            # Guard against config injection: newlines or semicolons would break
            # the generated Nginx/Apache config file.
            forbidden = ["\n", "\r", ";", '"', "\x00"]
            if any(char in v for char in forbidden):
                raise ValueError(
                    "node_socket path contains forbidden characters "
                    "(newlines, semicolons, quotes, or null bytes)."
                )
        return v

    @field_validator("php_socket")
    @classmethod
    def validate_php_socket(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.startswith("/"):
            raise ValueError(
                f"php_socket must be an absolute path (must start with '/'), got: '{v}'"
            )
        return v

    @model_validator(mode="after")
    def validate_ssl_fields(self) -> "VHostConfig":
        """Require cert_path and key_path when ssl_enabled is True."""
        if self.ssl_enabled:
            if self.cert_path is None:
                raise ValueError("cert_path is required when ssl_enabled=True")
            if self.key_path is None:
                raise ValueError("key_path is required when ssl_enabled=True")
        return self


class VHostInfo(BaseModel):
    domain: str = Field(..., description="The primary domain name of the virtual host")
    config_path: Path = Field(..., description="Absolute path to the virtual host configuration file")
    server_type: ServerType = Field(..., description="The web server type (Nginx or Apache)")
    status: str = Field(..., description="Status of the virtual host (Enabled, Disabled, or Unknown)")
    managed_by: str = Field(..., description="Indicates if the vhost is managed by 'VHost Helper' or 'External'")
    document_root: Optional[Path] = Field(None, description="The document root specified in the virtual host configuration")


class OSInfo(BaseModel):
    id: str = Field(..., description="Distribution ID (e.g., ubuntu)")
    version: str = Field(..., description="Version ID (e.g., 22.04)")
    family: str = Field(..., description="OS family (e.g., debian, rhel, arch)")
