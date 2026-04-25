"""
QA additions for ULTIMATE_VHOST-023 — ``vhost logs`` command.

Covers:
- Coverage gaps in ``logs.py`` lines 35 & 80 (standalone-comment ``continue``)
- Coverage gaps in ``main.py`` logs handler:
    - Domain validation failure (lines 1231-1233)
    - Provider-detection returns None (lines 1239-1242)
    - OSError/PermissionError reading config (lines 1263-1265)
    - ``--error`` flag when ErrorLog is absent from config (lines 1276-1279)
    - ``--access`` flag when CustomLog is absent from config (lines 1283-1286)
- Extra edge cases mandated by the PRD acceptance criteria but missing from
  the existing suite
"""

import re
import pytest
from typer.testing import CliRunner
from vhost_helper.logs import extract_nginx_log_paths, extract_apache_log_paths
from vhost_helper.main import app

def strip_ansi(text):
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nginx_conf(access_path, error_path):
    return (
        f"server {{\n"
        f"    access_log {access_path};\n"
        f"    error_log {error_path};\n"
        f"}}\n"
    )


def _apache_conf(access_path, error_path):
    return (
        f"<VirtualHost *:80>\n"
        f"    CustomLog {access_path} combined\n"
        f"    ErrorLog {error_path}\n"
        f"</VirtualHost>\n"
    )


@pytest.fixture
def mock_nginx_env(mocker, tmp_path):
    enabled = tmp_path / "nginx-enabled"
    enabled.mkdir()
    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", enabled)
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", enabled)
    mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-avail")
    mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled")
    mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)
    return enabled


@pytest.fixture
def mock_no_provider_env(mocker, tmp_path):
    """Both provider directories are absent so auto-detection returns None."""
    mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", tmp_path / "no-nginx-avail")
    mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "no-nginx-enabled")
    mocker.patch(
        "vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "no-apache-avail"
    )
    mocker.patch(
        "vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "no-apache-enabled"
    )
    mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
    mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)


# ===========================================================================
# Section 1 — logs.py pure-function edge cases (coverage gaps + extras)
# ===========================================================================


class TestNginxStandaloneComments:
    """Ensure standalone comment lines are skipped via ``continue`` (line 35)."""

    def test_standalone_comment_line_skipped(self):
        """A line that is only a comment becomes '' after stripping → continue."""
        config = (
            "# This is a standalone comment\n"
            "server {\n"
            "    # another comment\n"
            "    access_log /var/log/nginx/access.log;\n"
            "    error_log /var/log/nginx/error.log;\n"
            "}\n"
        )
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/access.log"
        assert error == "/var/log/nginx/error.log"

    def test_config_with_only_comments_returns_none_tuple(self):
        config = "# access_log /var/log/nginx/access.log;\n# error_log /var/log/nginx/error.log;\n"
        access, error = extract_nginx_log_paths(config)
        assert access is None
        assert error is None

    def test_hash_embedded_in_path_handled_safely(self):
        """Comment stripping must not eat the path itself."""
        config = "    access_log /var/log/nginx/access.log; # hash after semicolon\n"
        access, _ = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/access.log"

    def test_error_log_before_access_log(self):
        config = (
            "server {\n"
            "    error_log /var/log/nginx/error.log;\n"
            "    access_log /var/log/nginx/access.log;\n"
            "}\n"
        )
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/access.log"
        assert error == "/var/log/nginx/error.log"

    def test_windows_style_line_endings(self):
        config = "server {\r\n    access_log /var/log/nginx/access.log;\r\n    error_log /var/log/nginx/error.log;\r\n}\r\n"
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/access.log"
        assert error == "/var/log/nginx/error.log"

    def test_only_one_path_present_stops_scanning_at_both(self):
        """When both are found, scanning stops early without reading the rest."""
        config = (
            "    access_log /var/log/nginx/access.log;\n"
            "    error_log /var/log/nginx/error.log;\n"
            "    access_log /var/log/nginx/second_access.log;\n"  # must be ignored
        )
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/access.log"
        assert error == "/var/log/nginx/error.log"

    def test_access_log_off_upper_case(self):
        config = "    access_log OFF;\n    error_log /var/log/nginx/error.log;\n"
        access, error = extract_nginx_log_paths(config)
        assert access is None
        assert error == "/var/log/nginx/error.log"

    def test_error_log_off_mixed_case(self):
        config = "    access_log /var/log/nginx/access.log;\n    error_log Off;\n"
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/access.log"
        assert error is None

    def test_path_with_spaces_around_semicolon_is_stripped(self):
        """Semicolon is stripped but surrounding spaces in path are not pathological."""
        config = "    access_log /var/log/nginx/file.log combined;\n"
        access, _ = extract_nginx_log_paths(config)
        # The regex stops at whitespace, so 'combined;' is not included
        assert access == "/var/log/nginx/file.log"

    def test_multiple_server_blocks_uses_first_occurrence(self):
        config = (
            "server {\n"
            "    access_log /var/log/nginx/first.log;\n"
            "    error_log /var/log/nginx/first_err.log;\n"
            "}\n"
            "server {\n"
            "    access_log /var/log/nginx/second.log;\n"
            "    error_log /var/log/nginx/second_err.log;\n"
            "}\n"
        )
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/first.log"
        assert error == "/var/log/nginx/first_err.log"


class TestApacheStandaloneComments:
    """Ensure standalone comment lines are skipped via ``continue`` (line 80)."""

    def test_standalone_comment_line_skipped(self):
        config = (
            "# This is a comment\n"
            "<VirtualHost *:80>\n"
            "    # another comment\n"
            "    CustomLog /var/log/apache2/access.log combined\n"
            "    ErrorLog /var/log/apache2/error.log\n"
            "</VirtualHost>\n"
        )
        access, error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/access.log"
        assert error == "/var/log/apache2/error.log"

    def test_config_with_only_comments_returns_none_tuple(self):
        config = "# CustomLog /var/log/apache2/access.log combined\n# ErrorLog /var/log/apache2/error.log\n"
        access, error = extract_apache_log_paths(config)
        assert access is None
        assert error is None

    def test_errorlog_before_customlog(self):
        config = (
            "<VirtualHost *:80>\n"
            "    ErrorLog /var/log/apache2/error.log\n"
            "    CustomLog /var/log/apache2/access.log combined\n"
            "</VirtualHost>\n"
        )
        access, error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/access.log"
        assert error == "/var/log/apache2/error.log"

    def test_windows_style_line_endings(self):
        config = "<VirtualHost *:80>\r\n    CustomLog /var/log/apache2/access.log combined\r\n    ErrorLog /var/log/apache2/error.log\r\n</VirtualHost>\r\n"
        access, error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/access.log"
        assert error == "/var/log/apache2/error.log"

    def test_multiple_virtual_hosts_uses_first_occurrence(self):
        config = (
            "<VirtualHost *:80>\n"
            "    CustomLog /var/log/apache2/first.log combined\n"
            "    ErrorLog /var/log/apache2/first_err.log\n"
            "</VirtualHost>\n"
            "<VirtualHost *:443>\n"
            "    CustomLog /var/log/apache2/second.log combined\n"
            "    ErrorLog /var/log/apache2/second_err.log\n"
            "</VirtualHost>\n"
        )
        access, error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/first.log"
        assert error == "/var/log/apache2/first_err.log"

    def test_hash_in_line_comment_stripped_safely(self):
        config = "    CustomLog /var/log/apache2/access.log combined # prod\n"
        access, _ = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/access.log"

    def test_blank_lines_between_directives_are_skipped(self):
        config = (
            "\n"
            "    CustomLog /var/log/apache2/access.log combined\n"
            "\n"
            "    ErrorLog /var/log/apache2/error.log\n"
        )
        access, error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/access.log"
        assert error == "/var/log/apache2/error.log"


# ===========================================================================
# Section 2 — main.py logs command coverage gaps
# ===========================================================================


class TestLogsInvalidDomain:
    """Covers main.py lines 1231-1233: validate_domain raises ValueError."""

    def test_invalid_domain_double_dots_exits_1(self, mock_nginx_env):
        result = runner.invoke(app, ["logs", "invalid..domain"])
        assert result.exit_code == 1

    def test_invalid_domain_too_long_exits_1(self, mock_nginx_env):
        long_domain = "a" * 254 + ".test"
        result = runner.invoke(app, ["logs", long_domain])
        assert result.exit_code == 1

    def test_invalid_domain_special_chars_exits_1(self, mock_nginx_env):
        result = runner.invoke(app, ["logs", "invalid_domain!@#.test"])
        assert result.exit_code == 1


class TestLogsProviderDetectionReturnsNone:
    """Covers main.py lines 1239-1242: both provider dirs absent → returns None."""

    def test_no_provider_dirs_vhost_not_found(self, mock_no_provider_env):
        result = runner.invoke(app, ["logs", "ghost.test"])
        assert result.exit_code == 1
        assert "VHost not found or disabled: 'ghost.test'" in result.stdout

    def test_no_provider_dirs_apache_domain_not_found(self, mock_no_provider_env):
        result = runner.invoke(app, ["logs", "ghost.test", "--provider", "apache"])
        # Provider explicitly provided → skips auto-detection → hits enabled_path check
        assert result.exit_code == 1


class TestLogsOSErrorReadingConfig:
    """Covers main.py lines 1263-1265: OSError / PermissionError reading config."""

    def test_permission_error_reading_config_exits_1(self, mock_nginx_env, mocker):
        domain = "perm.test"
        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text("server {}\n")

        mocker.patch("pathlib.Path.read_text", side_effect=PermissionError("no access"))

        result = runner.invoke(app, ["logs", domain])
        assert result.exit_code == 1
        assert "Error reading configuration" in result.stdout

    def test_os_error_reading_config_exits_1(self, mock_nginx_env, mocker):
        domain = "oserr.test"
        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text("server {}\n")

        mocker.patch("pathlib.Path.read_text", side_effect=OSError("I/O error"))

        result = runner.invoke(app, ["logs", domain])
        assert result.exit_code == 1
        assert "Error reading configuration" in result.stdout


class TestLogsErrorFlagNoErrorLogInConfig:
    """Covers main.py lines 1276-1279: --error but no ErrorLog directive found."""

    def test_error_flag_no_error_log_directive_nginx(self, mock_nginx_env):
        domain = "noerrlog.test"
        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text(
            "server {\n"
            "    access_log /var/log/nginx/access.log;\n"
            "    # no error_log directive\n"
            "}\n"
        )
        result = runner.invoke(app, ["logs", domain, "--error"])
        assert result.exit_code == 1
        assert f"No log paths found in configuration for '{domain}'" in result.stdout

    def test_error_flag_error_log_off_nginx(self, mock_nginx_env):
        domain = "erroff.test"
        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text(
            "server {\n"
            "    access_log /var/log/nginx/access.log;\n"
            "    error_log off;\n"
            "}\n"
        )
        result = runner.invoke(app, ["logs", domain, "--error"])
        assert result.exit_code == 1
        assert f"No log paths found in configuration for '{domain}'" in result.stdout


class TestLogsAccessFlagNoAccessLogInConfig:
    """Covers main.py lines 1283-1286: --access but no CustomLog/access_log directive."""

    def test_access_flag_no_access_log_directive_nginx(self, mock_nginx_env):
        domain = "noaccesslog.test"
        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text(
            "server {\n"
            "    error_log /var/log/nginx/error.log;\n"
            "    # no access_log directive\n"
            "}\n"
        )
        result = runner.invoke(app, ["logs", domain, "--access"])
        assert result.exit_code == 1
        assert f"No log paths found in configuration for '{domain}'" in result.stdout

    def test_access_flag_access_log_off_nginx(self, mock_nginx_env):
        domain = "accessoff.test"
        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text(
            "server {\n"
            "    access_log off;\n"
            "    error_log /var/log/nginx/error.log;\n"
            "}\n"
        )
        result = runner.invoke(app, ["logs", domain, "--access"])
        assert result.exit_code == 1
        assert f"No log paths found in configuration for '{domain}'" in result.stdout


# ===========================================================================
# Section 3 — Additional edge cases from PRD acceptance criteria
# ===========================================================================


class TestLogsPartialLogPaths:
    """Default mode with only one log path present should tail the available one."""

    def test_only_access_log_present_in_default_mode(
        self, mock_nginx_env, tmp_path, mocker
    ):
        """If error_log is absent, default mode tails only access log without error."""
        domain = "onlyaccess.test"
        access_log = tmp_path / "access.log"
        access_log.touch()

        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text(f"server {{\n" f"    access_log {access_log};\n" f"}}\n")
        mocker.patch("shutil.which", return_value="/usr/bin/tail")
        mock_popen = mocker.patch("subprocess.Popen")
        mock_popen.return_value.wait.return_value = 0

        result = runner.invoke(app, ["logs", domain])
        assert result.exit_code == 0
        call_args = mock_popen.call_args[0][0]
        assert str(access_log) in call_args

    def test_only_error_log_present_in_default_mode(
        self, mock_nginx_env, tmp_path, mocker
    ):
        """If access_log is absent, default mode tails only error log without error."""
        domain = "onlyerror.test"
        error_log = tmp_path / "error.log"
        error_log.touch()

        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text(f"server {{\n" f"    error_log {error_log};\n" f"}}\n")
        mocker.patch("shutil.which", return_value="/usr/bin/tail")
        mock_popen = mocker.patch("subprocess.Popen")
        mock_popen.return_value.wait.return_value = 0

        result = runner.invoke(app, ["logs", domain])
        assert result.exit_code == 0
        call_args = mock_popen.call_args[0][0]
        assert str(error_log) in call_args

    def test_access_log_off_error_log_exists_default_mode(
        self, mock_nginx_env, tmp_path, mocker
    ):
        """access_log off means only error_log is tailed in default mode."""
        domain = "accessoff2.test"
        error_log = tmp_path / "error.log"
        error_log.touch()

        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text(
            f"server {{\n"
            f"    access_log off;\n"
            f"    error_log {error_log};\n"
            f"}}\n"
        )
        mocker.patch("shutil.which", return_value="/usr/bin/tail")
        mock_popen = mocker.patch("subprocess.Popen")
        mock_popen.return_value.wait.return_value = 0

        result = runner.invoke(app, ["logs", domain])
        assert result.exit_code == 0
        call_args = mock_popen.call_args[0][0]
        assert str(error_log) in call_args


class TestLogsErrorFilesMissing:
    """PRD AC: 'Log file not found at [PATH]' when file deleted after provisioning."""

    def test_error_log_file_missing_default_mode(
        self, mock_nginx_env, tmp_path, mocker
    ):
        """Both paths discovered but error_log file missing → exit 1 with path in message."""
        domain = "missingerr.test"
        access_log = tmp_path / "access.log"
        error_log = tmp_path / "error.log"
        access_log.touch()
        # error_log intentionally not created

        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text(_nginx_conf(str(access_log), str(error_log)))
        mocker.patch("shutil.which", return_value="/usr/bin/tail")

        result = runner.invoke(app, ["logs", domain])
        assert result.exit_code == 1
        assert "Log file not found at" in result.stdout

    def test_apache_log_file_missing_exit_1(self, mocker, tmp_path):
        enabled = tmp_path / "apache-enabled"
        enabled.mkdir()
        mocker.patch(
            "vhost_helper.main.NGINX_SITES_AVAILABLE", tmp_path / "nginx-avail"
        )
        mocker.patch(
            "vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nginx-enabled"
        )
        mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", enabled)
        mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", enabled)
        mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
        mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)

        domain = "apachegone.test"
        access_log = tmp_path / "access.log"
        error_log = tmp_path / "error.log"
        # Neither file exists

        conf = enabled / f"{domain}.conf"
        conf.write_text(_apache_conf(str(access_log), str(error_log)))
        mocker.patch("shutil.which", return_value="/usr/bin/tail")

        result = runner.invoke(app, ["logs", domain])
        assert result.exit_code == 1
        assert "Log file not found at" in result.stdout


class TestLogsTailCommandStructure:
    """Structural and security checks on the subprocess invocation."""

    def test_tail_invoked_with_shell_false_apache(self, mocker, tmp_path):
        """Regression: shell=False must hold for Apache paths too."""
        enabled = tmp_path / "apache-enabled"
        enabled.mkdir()
        mocker.patch(
            "vhost_helper.main.NGINX_SITES_AVAILABLE", tmp_path / "nginx-avail"
        )
        mocker.patch(
            "vhost_helper.main.NGINX_SITES_ENABLED", tmp_path / "nginx-enabled"
        )
        mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", enabled)
        mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", enabled)
        mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
        mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)

        domain = "shell.test"
        access_log = tmp_path / "access.log"
        error_log = tmp_path / "error.log"
        access_log.touch()
        error_log.touch()

        conf = enabled / f"{domain}.conf"
        conf.write_text(_apache_conf(str(access_log), str(error_log)))

        mocker.patch("shutil.which", return_value="/usr/bin/tail")
        mock_popen = mocker.patch("subprocess.Popen")
        mock_popen.return_value.wait.return_value = 0

        runner.invoke(app, ["logs", domain])

        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("shell") is not True

    def test_tail_args_start_with_tail_f(self, mock_nginx_env, tmp_path, mocker):
        """Subprocess command always starts with [tail_bin, '-f']."""
        domain = "startswith.test"
        access_log = tmp_path / "access.log"
        error_log = tmp_path / "error.log"
        access_log.touch()
        error_log.touch()

        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text(_nginx_conf(str(access_log), str(error_log)))

        mocker.patch("shutil.which", return_value="/usr/bin/tail")
        mock_popen = mocker.patch("subprocess.Popen")
        mock_popen.return_value.wait.return_value = 0

        runner.invoke(app, ["logs", domain])

        call_args = mock_popen.call_args[0][0]
        assert call_args[0] == "/usr/bin/tail"
        assert call_args[1] == "-f"

    def test_provider_override_nginx_on_apache_domain(self, mocker, tmp_path):
        """--provider nginx on an Apache-configured domain uses Nginx path lookup."""
        nginx_enabled = tmp_path / "nginx-enabled"
        nginx_enabled.mkdir()
        apache_enabled = tmp_path / "apache-enabled"
        apache_enabled.mkdir()

        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", nginx_enabled)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", nginx_enabled)
        mocker.patch("vhost_helper.main.APACHE_SITES_AVAILABLE", apache_enabled)
        mocker.patch("vhost_helper.main.APACHE_SITES_ENABLED", apache_enabled)
        mocker.patch("vhost_helper.main.NGINX_SITES_DISABLED", None)
        mocker.patch("vhost_helper.main.APACHE_SITES_DISABLED", None)

        domain = "cross.test"
        access_log = tmp_path / "access.log"
        error_log = tmp_path / "error.log"
        access_log.touch()
        error_log.touch()

        # Write Nginx config (--provider nginx takes precedence)
        conf = nginx_enabled / f"{domain}.conf"
        conf.write_text(_nginx_conf(str(access_log), str(error_log)))

        mocker.patch("shutil.which", return_value="/usr/bin/tail")
        mock_popen = mocker.patch("subprocess.Popen")
        mock_popen.return_value.wait.return_value = 0

        result = runner.invoke(app, ["logs", domain, "--provider", "nginx"])
        assert result.exit_code == 0
        call_args = mock_popen.call_args[0][0]
        assert str(access_log) in call_args
        assert str(error_log) in call_args

    def test_popen_called_once_per_invocation(self, mock_nginx_env, tmp_path, mocker):
        """Exactly one subprocess is launched per ``vhost logs`` invocation."""
        domain = "once.test"
        access_log = tmp_path / "access.log"
        error_log = tmp_path / "error.log"
        access_log.touch()
        error_log.touch()

        conf = mock_nginx_env / f"{domain}.conf"
        conf.write_text(_nginx_conf(str(access_log), str(error_log)))

        mocker.patch("shutil.which", return_value="/usr/bin/tail")
        mock_popen = mocker.patch("subprocess.Popen")
        mock_popen.return_value.wait.return_value = 0

        runner.invoke(app, ["logs", domain])
        assert mock_popen.call_count == 1


class TestLogsHelpText:
    """Regression: ``vhost logs --help`` must exit 0 and mention expected flags."""

    def test_logs_help_exits_zero(self):
        result = runner.invoke(app, ["logs", "--help"])
        assert result.exit_code == 0

    def test_logs_help_mentions_error_flag(self):
        result = runner.invoke(app, ["logs", "--help"])
        assert "--error" in strip_ansi(result.stdout)

    def test_logs_help_mentions_access_flag(self):
        result = runner.invoke(app, ["logs", "--help"])
        assert "--access" in strip_ansi(result.stdout)

    def test_logs_help_mentions_provider_flag(self):
        result = runner.invoke(app, ["logs", "--help"])
        assert "--provider" in strip_ansi(result.stdout)
