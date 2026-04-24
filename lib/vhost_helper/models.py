from pydantic import BaseModel, Field, field_validator
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


# Default php-fpm socket paths keyed by OS family
PHP_SOCKET_PATHS: dict[str, str] = {
    "debian": "/run/php/php-fpm.sock",
    "rhel": "/run/php-fpm/www.sock",
    "arch": "/run/php-fpm/php-fpm.sock",
}
DEFAULT_PHP_SOCKET = "/run/php/php-fpm.sock"


class VHostConfig(BaseModel):
    domain: str = Field(..., description="The domain name (e.g., mysite.test)", pattern=r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$")
    document_root: Path = Field(..., description="The absolute path to the project root")
    server_type: ServerType = Field(default=ServerType.NGINX, description="The web server type")
    enabled: bool = Field(default=True, description="Current status of the virtual host")
    port: int = Field(default=80, description="Port number", ge=1, le=65535)
    runtime: RuntimeMode = Field(default=RuntimeMode.STATIC, description="Language runtime mode")
    python_port: int = Field(default=8000, description="Gunicorn upstream port (Python runtime)", ge=1, le=65535)
    node_port: int = Field(default=3000, description="Node.js upstream port (nodejs runtime)", ge=1, le=65535)
    node_socket: Optional[str] = Field(default=None, description="Unix Domain Socket path for Node.js (overrides node_port when set)")
    php_socket: str = Field(default=DEFAULT_PHP_SOCKET, description="PHP-FPM socket path (PHP runtime)")
    template: str = Field(default="default", description="The template used to create the config")

    @field_validator("document_root")
    @classmethod
    def validate_document_root(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(f"Document root {v} does not exist.")
        if not v.is_dir():
            raise ValueError(f"Document root {v} must be a directory.")
        
        # Security: Prevent configuration injection in Nginx/Apache templates.
        # Check for characters that could break out of a double-quoted string.
        forbidden = ['"', '\n', '\r']
        if any(char in str(v) for char in forbidden):
            raise ValueError("Document root path contains forbidden characters (quotes or newlines).")
            
        return v


class OSInfo(BaseModel):
    id: str = Field(..., description="Distribution ID (e.g., ubuntu)")
    version: str = Field(..., description="Version ID (e.g., 22.04)")
    family: str = Field(..., description="OS family (e.g., debian, rhel, arch)")
