"""Unit tests for the php_fpm discovery and service orchestration module.

Covers:
- resolve_socket_path() for Debian and RHEL families
- detect_default_version() with mocked filesystem and subprocess
- validate_version_present() success and PhpFpmNotFoundError paths
- get_service_name() for Debian and RHEL
- start_service() success, failure, and systemctl-not-found paths
- _parse_php_version_from_output() helper
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from vhost_helper.php_fpm import (
    PhpFpmNotFoundError,
    _parse_php_version_from_output,
    detect_default_version,
    get_service_name,
    resolve_socket_path,
    start_service,
    validate_version_present,
)

# ---------------------------------------------------------------------------
# resolve_socket_path
# ---------------------------------------------------------------------------


class TestResolveSocketPath:
    def test_debian_returns_versioned_path(self):
        result = resolve_socket_path("8.2", "debian_family")
        assert result == "/run/php/php8.2-fpm.sock"

    def test_debian_returns_versioned_path_7_4(self):
        result = resolve_socket_path("7.4", "debian_family")
        assert result == "/run/php/php7.4-fpm.sock"

    def test_rhel_returns_version_agnostic_path(self):
        result = resolve_socket_path("8.2", "rhel_family")
        assert result == "/run/php-fpm/www.sock"

    def test_rhel_path_is_same_for_all_versions(self):
        assert resolve_socket_path("7.4", "rhel_family") == resolve_socket_path(
            "8.1", "rhel_family"
        )

    def test_unknown_family_treated_as_debian(self):
        """Unknown OS families fall through to the debian path."""
        result = resolve_socket_path("8.1", "arch_family")
        assert result == "/run/php/php8.1-fpm.sock"


# ---------------------------------------------------------------------------
# get_service_name
# ---------------------------------------------------------------------------


class TestGetServiceName:
    def test_debian_returns_versioned_service(self):
        assert get_service_name("8.2", "debian_family") == "php8.2-fpm"

    def test_rhel_returns_plain_service(self):
        assert get_service_name("8.2", "rhel_family") == "php-fpm"

    def test_debian_7_4(self):
        assert get_service_name("7.4", "debian_family") == "php7.4-fpm"


# ---------------------------------------------------------------------------
# _parse_php_version_from_output
# ---------------------------------------------------------------------------


class TestParsePhpVersionFromOutput:
    def test_standard_output(self):
        output = "PHP 8.2.7 (cli) (built: Jun  9 2023 07:26:57)\n"
        assert _parse_php_version_from_output(output) == "8.2"

    def test_7_4_output(self):
        output = "PHP 7.4.33 (cli)\n"
        assert _parse_php_version_from_output(output) == "7.4"

    def test_returns_none_on_garbage(self):
        assert _parse_php_version_from_output("nothing useful here") is None

    def test_empty_string(self):
        assert _parse_php_version_from_output("") is None


# ---------------------------------------------------------------------------
# detect_default_version (Debian path)
# ---------------------------------------------------------------------------


class TestDetectDefaultVersionDebian:
    def test_picks_highest_from_sockets(self):
        with (
            patch(
                "vhost_helper.php_fpm.glob.glob",
                return_value=[
                    "/run/php/php8.1-fpm.sock",
                    "/run/php/php8.2-fpm.sock",
                ],
            ),
            patch("vhost_helper.php_fpm.shutil.which", return_value=None),
        ):
            result = detect_default_version("debian_family")
        assert result == "8.2"

    def test_picks_only_socket_when_one(self):
        with (
            patch(
                "vhost_helper.php_fpm.glob.glob",
                return_value=["/run/php/php8.1-fpm.sock"],
            ),
            patch("vhost_helper.php_fpm.shutil.which", return_value=None),
        ):
            result = detect_default_version("debian_family")
        assert result == "8.1"

    def test_probes_php_binary_when_no_sockets(self):
        mock_result = MagicMock()
        mock_result.stdout = "PHP 8.2.7 (cli)\n"
        with (
            patch("vhost_helper.php_fpm.glob.glob", return_value=[]),
            patch("vhost_helper.php_fpm.shutil.which", return_value="/usr/bin/php"),
            patch("vhost_helper.php_fpm.subprocess.run", return_value=mock_result),
        ):
            result = detect_default_version("debian_family")
        assert result == "8.2"

    def test_raises_when_no_candidates_found(self):
        with (
            patch("vhost_helper.php_fpm.glob.glob", return_value=[]),
            patch("vhost_helper.php_fpm.shutil.which", return_value=None),
        ):
            with pytest.raises(PhpFpmNotFoundError) as exc_info:
                detect_default_version("debian_family")
        assert "Debian/Ubuntu" in str(exc_info.value)

    def test_deduplicates_candidates(self):
        """If socket and binary report the same version, it appears once."""
        mock_result = MagicMock()
        mock_result.stdout = "PHP 8.2.7 (cli)\n"
        with (
            patch(
                "vhost_helper.php_fpm.glob.glob",
                return_value=["/run/php/php8.2-fpm.sock"],
            ),
            patch("vhost_helper.php_fpm.shutil.which", return_value="/usr/bin/php"),
            patch("vhost_helper.php_fpm.subprocess.run", return_value=mock_result),
        ):
            result = detect_default_version("debian_family")
        assert result == "8.2"

    def test_ignores_parse_failure_from_binary(self):
        """If php --version output is unparseable, binary is skipped."""
        mock_result = MagicMock()
        mock_result.stdout = "unrecognised"
        with (
            patch(
                "vhost_helper.php_fpm.glob.glob",
                return_value=["/run/php/php8.1-fpm.sock"],
            ),
            patch("vhost_helper.php_fpm.shutil.which", return_value="/usr/bin/php"),
            patch("vhost_helper.php_fpm.subprocess.run", return_value=mock_result),
        ):
            result = detect_default_version("debian_family")
        assert result == "8.1"

    def test_tolerates_subprocess_timeout(self):
        with (
            patch(
                "vhost_helper.php_fpm.glob.glob",
                return_value=["/run/php/php8.1-fpm.sock"],
            ),
            patch("vhost_helper.php_fpm.shutil.which", return_value="/usr/bin/php"),
            patch(
                "vhost_helper.php_fpm.subprocess.run",
                side_effect=subprocess.TimeoutExpired("php", 5),
            ),
        ):
            result = detect_default_version("debian_family")
        assert result == "8.1"


# ---------------------------------------------------------------------------
# detect_default_version (RHEL path)
# ---------------------------------------------------------------------------


class TestDetectDefaultVersionRhel:
    def test_returns_system_when_socket_exists(self):
        with patch("vhost_helper.php_fpm.os.path.exists", return_value=True):
            result = detect_default_version("rhel_family")
        assert result == "system"

    def test_returns_system_when_binary_found(self):
        with (
            patch("vhost_helper.php_fpm.os.path.exists", return_value=False),
            patch(
                "vhost_helper.php_fpm.shutil.which", return_value="/usr/sbin/php-fpm"
            ),
        ):
            result = detect_default_version("rhel_family")
        assert result == "system"

    def test_raises_when_nothing_found(self):
        with (
            patch("vhost_helper.php_fpm.os.path.exists", return_value=False),
            patch("vhost_helper.php_fpm.shutil.which", return_value=None),
        ):
            with pytest.raises(PhpFpmNotFoundError) as exc_info:
                detect_default_version("rhel_family")
        assert "RHEL" in str(exc_info.value)


# ---------------------------------------------------------------------------
# validate_version_present
# ---------------------------------------------------------------------------


class TestValidateVersionPresent:
    def test_debian_success_via_socket(self):
        with patch("vhost_helper.php_fpm.os.path.exists", return_value=True):
            result = validate_version_present("8.2", "debian_family")
        assert result == "/run/php/php8.2-fpm.sock"

    def test_debian_success_via_binary(self):
        with (
            patch("vhost_helper.php_fpm.os.path.exists", return_value=False),
            patch(
                "vhost_helper.php_fpm.shutil.which",
                return_value="/usr/sbin/php8.2-fpm",
            ),
        ):
            result = validate_version_present("8.2", "debian_family")
        assert result == "/run/php/php8.2-fpm.sock"

    def test_debian_raises_when_absent(self):
        with (
            patch("vhost_helper.php_fpm.os.path.exists", return_value=False),
            patch("vhost_helper.php_fpm.shutil.which", return_value=None),
        ):
            with pytest.raises(PhpFpmNotFoundError) as exc_info:
                validate_version_present("7.4", "debian_family")
        error_msg = str(exc_info.value)
        assert "7.4" in error_msg
        assert "/run/php/php7.4-fpm.sock" in error_msg

    def test_rhel_success_via_socket(self):
        with patch("vhost_helper.php_fpm.os.path.exists", return_value=True):
            result = validate_version_present("8.2", "rhel_family")
        assert result == "/run/php-fpm/www.sock"

    def test_rhel_raises_when_absent(self):
        with (
            patch("vhost_helper.php_fpm.os.path.exists", return_value=False),
            patch("vhost_helper.php_fpm.shutil.which", return_value=None),
        ):
            with pytest.raises(PhpFpmNotFoundError) as exc_info:
                validate_version_present("8.2", "rhel_family")
        assert "8.2" in str(exc_info.value)


# ---------------------------------------------------------------------------
# start_service
# ---------------------------------------------------------------------------


class TestStartService:
    def test_returns_none_on_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("vhost_helper.php_fpm.subprocess.run", return_value=mock_result):
            result = start_service("8.2", "debian_family")
        assert result is None

    def test_returns_warning_on_nonzero_exit(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("vhost_helper.php_fpm.subprocess.run", return_value=mock_result):
            result = start_service("8.2", "debian_family")
        assert result is not None
        assert "php8.2-fpm" in result
        assert "1" in result

    def test_returns_warning_when_systemctl_not_found(self):
        with patch(
            "vhost_helper.php_fpm.subprocess.run",
            side_effect=FileNotFoundError("systemctl not found"),
        ):
            result = start_service("8.2", "debian_family")
        assert result is not None
        assert "systemctl" in result.lower()

    def test_rhel_uses_plain_service_name(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch(
            "vhost_helper.php_fpm.subprocess.run", return_value=mock_result
        ) as mock_run:
            start_service("8.2", "rhel_family")
        call_args = mock_run.call_args[0][0]
        assert "php-fpm" in call_args
        assert "php8.2-fpm" not in " ".join(call_args)

    def test_debian_uses_versioned_service_name(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch(
            "vhost_helper.php_fpm.subprocess.run", return_value=mock_result
        ) as mock_run:
            start_service("8.2", "debian_family")
        call_args = mock_run.call_args[0][0]
        assert "php8.2-fpm" in call_args

    def test_systemctl_called_without_shell(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch(
            "vhost_helper.php_fpm.subprocess.run", return_value=mock_result
        ) as mock_run:
            start_service("8.2", "debian_family")
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs.get("shell", False) is False or "shell" not in call_kwargs

    def test_returns_warning_on_oserror(self):
        with patch(
            "vhost_helper.php_fpm.subprocess.run",
            side_effect=OSError("permission denied"),
        ):
            result = start_service("8.2", "debian_family")
        assert result is not None
