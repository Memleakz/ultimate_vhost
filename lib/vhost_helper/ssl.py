import os
import shutil
import subprocess
from pathlib import Path

DEFAULT_SSL_DIR = "/etc/vhost-helper/ssl"
SSL_DIR_ENV_VAR = "VHOST_SSL_DIR"

MKCERT_NOT_FOUND_MSG = (
    "mkcert binary not found. Install it with:\n"
    "  Debian/Ubuntu: sudo apt install mkcert\n"
    "  Fedora/RHEL:   sudo dnf install mkcert\n"
    "  Upstream:      https://github.com/FiloSottile/mkcert"
)


def get_ssl_dir(cli_ssl_dir: str | None = None) -> Path:
    """Resolve certificate directory with CLI > env var > default precedence."""
    if cli_ssl_dir is not None:
        return Path(cli_ssl_dir)
    env_val = os.getenv(SSL_DIR_ENV_VAR)
    if env_val:
        return Path(env_val)
    return Path(DEFAULT_SSL_DIR)


def check_mkcert_binary() -> str:
    """Return the absolute path to the mkcert binary.

    Raises RuntimeError with MKCERT_NOT_FOUND_MSG when the binary is absent
    from PATH so callers can surface actionable installation instructions.
    """
    path = shutil.which("mkcert")
    if path is None:
        raise RuntimeError(MKCERT_NOT_FOUND_MSG)
    return path


def ensure_ssl_dir(ssl_dir: Path) -> None:
    """Create the SSL directory with mode 0750 if it does not exist."""
    ssl_dir.mkdir(parents=True, exist_ok=True)
    ssl_dir.chmod(0o750)


def generate_certificate(domain: str, ssl_dir: Path) -> tuple[Path, Path]:
    """Generate a locally-trusted certificate for *domain* using mkcert.

    mkcert is invoked with ``shell=False`` and the configured SSL directory
    as the working directory.  The canonical output files are:
        <ssl_dir>/<domain>.pem
        <ssl_dir>/<domain>-key.pem

    If mkcert writes a differently-named file (e.g. ``<domain>+0.pem``), it is
    renamed to the canonical form before this function returns.

    Returns:
        (cert_path, key_path) as absolute Path objects.

    Raises:
        RuntimeError: when the mkcert binary is absent, when the subprocess
            exits non-zero, or when the expected output files cannot be located.
    """
    mkcert_bin = check_mkcert_binary()
    ensure_ssl_dir(ssl_dir)

    cert_path = ssl_dir / f"{domain}.pem"
    key_path = ssl_dir / f"{domain}-key.pem"

    result = subprocess.run(
        [mkcert_bin, domain],
        cwd=str(ssl_dir),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"mkcert failed for domain '{domain}': {result.stderr.strip()}"
        )

    # mkcert may use legacy naming when the domain contains special characters.
    # Normalise to <domain>.pem / <domain>-key.pem if required.
    if not cert_path.exists():
        legacy_cert = ssl_dir / f"{domain}+0.pem"
        if legacy_cert.exists():
            legacy_cert.rename(cert_path)
        else:
            raise RuntimeError(
                f"mkcert succeeded but certificate file not found at {cert_path}"
            )

    if not key_path.exists():
        legacy_key = ssl_dir / f"{domain}+0-key.pem"
        if legacy_key.exists():
            legacy_key.rename(key_path)
        else:
            raise RuntimeError(
                f"mkcert succeeded but key file not found at {key_path}"
            )

    cert_path.chmod(0o640)
    key_path.chmod(0o640)

    return cert_path, key_path
