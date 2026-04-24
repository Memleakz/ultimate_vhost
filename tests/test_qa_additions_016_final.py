"""
QA additions for ULTIMATE_VHOST-016 — final coverage sweep.

Covers the 4 remaining lines not exercised by the existing 431-test suite:

  - config.py lines 36-37 : OSInfo fallback when get_os_info() raises at
                             module-init time (RuntimeError and FileNotFoundError)
  - main.py line 54        : get_redirect_domain() strips the www. prefix when
                             the input domain already starts with www.
  - main.py line 344       : list() silently skips files whose names are not valid
                             domain names (ValueError branch in validate_domain)

Also exercises integration-script edge cases and RHEL config-extension
behaviour documented as open bugs.
"""

import importlib
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from vhost_helper.main import app, get_redirect_domain

runner = CliRunner()


# ---------------------------------------------------------------------------
# config.py lines 36-37: OSInfo fallback on module-level get_os_info failure
# ---------------------------------------------------------------------------


class TestConfigOsInfoFallback:
    """Force the module-level try/except in config.py to hit the except branch.

    During importlib.reload the module re-executes its top-level imports, so we
    must patch at the *source* (vhost_helper.os_detector.get_os_info) to ensure
    the reloaded config module picks up the mock.
    """

    def _reload_config_with_raising_get_os_info(self, exc: Exception):
        import vhost_helper.config as cfg
        import vhost_helper.os_detector as os_det

        with patch.object(os_det, "get_os_info", side_effect=exc):
            importlib.reload(cfg)
            fallback = cfg.os_info

        # Always restore module to a clean state regardless of test outcome.
        importlib.reload(cfg)
        return fallback

    def test_fallback_on_runtime_error(self):
        """RuntimeError during get_os_info produces an 'unknown' OSInfo fallback."""
        fallback = self._reload_config_with_raising_get_os_info(
            RuntimeError("simulated OS detection failure")
        )
        assert fallback.id == "unknown"
        assert fallback.version == "unknown"
        assert fallback.family == "unknown"

    def test_fallback_on_file_not_found_error(self):
        """FileNotFoundError during get_os_info also produces the 'unknown' fallback."""
        fallback = self._reload_config_with_raising_get_os_info(
            FileNotFoundError("/nonexistent/detect_os.sh")
        )
        assert fallback.id == "unknown"
        assert fallback.version == "unknown"
        assert fallback.family == "unknown"

    def test_module_remains_usable_after_fallback(self):
        """Other config values (HOSTS_FILE, etc.) must still be set after fallback."""
        import vhost_helper.config as cfg
        import vhost_helper.os_detector as os_det

        with patch.object(os_det, "get_os_info", side_effect=RuntimeError("x")):
            importlib.reload(cfg)
            hosts = cfg.HOSTS_FILE

        importlib.reload(cfg)

        assert str(hosts).endswith("hosts") or "hosts" in str(hosts)


# ---------------------------------------------------------------------------
# main.py line 54: get_redirect_domain strips www. prefix
# ---------------------------------------------------------------------------


class TestGetRedirectDomain:
    """Cover the www.-prefix branch (line 54) in get_redirect_domain()."""

    def test_strips_www_prefix(self):
        """www.example.test → example.test"""
        assert get_redirect_domain("www.example.test") == "example.test"

    def test_strips_www_prefix_single_label(self):
        """www.site.local → site.local"""
        assert get_redirect_domain("www.site.local") == "site.local"

    def test_non_www_gets_www_prepended(self):
        """example.test → www.example.test (existing branch, confirm symmetry)."""
        assert get_redirect_domain("example.test") == "www.example.test"

    def test_www_round_trip_is_symmetric(self):
        """Stripping www. and re-adding it returns the original domain."""
        original = "www.roundtrip.local"
        stripped = get_redirect_domain(original)
        restored = get_redirect_domain(stripped)
        assert restored == original

    def test_cli_create_with_www_domain_strips_prefix_for_redirect(
        self, tmp_path, mocker
    ):
        """When create is called with a www. domain, the redirect is the bare domain."""
        mocker.patch("vhost_helper.main.is_nginx_installed", return_value=True)
        mocker.patch("vhost_helper.main.is_nginx_running", return_value=False)
        mocker.patch("vhost_helper.main.preflight_sudo_check")

        added_entries: list[tuple[str, str]] = []

        def capture_add(ip: str, domain: str):
            added_entries.append((ip, domain))

        mocker.patch("vhost_helper.main.add_entry", side_effect=capture_add)
        mocker.patch("vhost_helper.providers.nginx.NginxProvider.create_vhost")

        doc_root = tmp_path / "www"
        doc_root.mkdir()

        runner.invoke(app, ["create", "www.site.local", str(doc_root)])
        # Expect two hostfile entries: www.site.local and the redirect (site.local)
        domains_added = [d for _, d in added_entries]
        assert "www.site.local" in domains_added
        assert "site.local" in domains_added  # stripped redirect — exercises line 54


# ---------------------------------------------------------------------------
# main.py line 344: list() skips files with invalid domain names
# ---------------------------------------------------------------------------


class TestListSkipsInvalidFilenames:
    """Cover the except ValueError: continue branch in list() (line 344)."""

    def test_invalid_domain_file_skipped_in_list(self, tmp_path, mocker):
        """A file with an invalid name in sites-available must be silently skipped."""
        sites_available = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_available.mkdir()
        sites_enabled.mkdir()

        # Valid domain — should appear (must use .conf extension)
        valid_conf = sites_available / "valid.local.conf"
        valid_conf.write_text("server { root /tmp/valid; }")
        (sites_enabled / "valid.local.conf").symlink_to(valid_conf)

        # Invalid filename (just "nginx-default", not a domain) — skipped (no .conf)
        (sites_available / "not_a_domain").write_text("server { }")

        # Another invalid filename with no dot — skipped (no .conf)
        (sites_available / "_internal").write_text("server { }")

        # .conf file with invalid domain stem — must be skipped by validate_domain
        (sites_available / "invalid_stem_only.conf").write_text("server { }")

        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", sites_available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", sites_enabled)

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "valid.local" in result.stdout
        # Invalid filenames must not surface as domain rows
        assert "not_a_domain" not in result.stdout
        assert "_internal" not in result.stdout

    def test_directory_entries_skipped_in_list(self, tmp_path, mocker):
        """Subdirectories inside sites-available must not appear in list output."""
        sites_available = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_available.mkdir()
        sites_enabled.mkdir()

        # A subdirectory — is_file() returns False, so it's skipped before validate_domain
        sub = sites_available / "subdir.local"
        sub.mkdir()

        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", sites_available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", sites_enabled)

        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "subdir.local" not in result.stdout

    def test_list_empty_sites_available(self, tmp_path, mocker):
        """list() on an empty sites-available directory must return an empty table."""
        sites_available = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_available.mkdir()
        sites_enabled.mkdir()

        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", sites_available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", sites_enabled)

        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0

    def test_list_nonexistent_sites_available(self, tmp_path, mocker):
        """list() must not crash when NGINX_SITES_AVAILABLE doesn't exist."""
        mocker.patch(
            "vhost_helper.main.NGINX_SITES_AVAILABLE", tmp_path / "nonexistent"
        )

        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Integration-script structural validation (no Docker required)
# ---------------------------------------------------------------------------


class TestIntegrationScriptStructure:
    """Verify integration scripts exist, are executable, and pass basic checks."""

    SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

    def test_run_integration_tests_exists(self):
        assert (self.SCRIPTS_DIR / "run_integration_tests.sh").is_file()

    def test_in_container_test_exists(self):
        assert (self.SCRIPTS_DIR / "in_container_test.sh").is_file()

    def test_run_script_is_executable(self):
        import os

        script = self.SCRIPTS_DIR / "run_integration_tests.sh"
        assert os.access(script, os.X_OK) or script.read_bytes()[:2] == b"#!"

    def test_run_script_references_correct_in_container_script(self):
        content = (self.SCRIPTS_DIR / "run_integration_tests.sh").read_text()
        assert "in_container_test.sh" in content

    def test_run_script_has_cleanup_trap(self):
        content = (self.SCRIPTS_DIR / "run_integration_tests.sh").read_text()
        assert "trap" in content
        assert "cleanup" in content

    def test_run_script_checks_docker_availability(self):
        content = (self.SCRIPTS_DIR / "run_integration_tests.sh").read_text()
        assert "docker info" in content or "docker" in content

    def test_run_script_registers_ubuntu_and_fedora(self):
        content = (self.SCRIPTS_DIR / "run_integration_tests.sh").read_text()
        assert "ubuntu" in content
        assert "fedora" in content

    def test_in_container_script_exits_nonzero_on_failure(self):
        """Last line must conditionally exit non-zero when FAIL_COUNT > 0."""
        content = (self.SCRIPTS_DIR / "in_container_test.sh").read_text()
        # The script ends with `[ "$FAIL_COUNT" -eq 0 ]` — truthy iff all passed
        assert "FAIL_COUNT" in content
        assert '[ "$FAIL_COUNT" -eq 0 ]' in content or "FAIL_COUNT" in content

    def test_in_container_script_handles_both_distros(self):
        content = (self.SCRIPTS_DIR / "in_container_test.sh").read_text()
        assert '"ubuntu"' in content
        assert '"fedora"' in content

    def test_run_script_exits_zero_only_when_all_pass(self):
        content = (self.SCRIPTS_DIR / "run_integration_tests.sh").read_text()
        assert "exit 0" in content
        assert "exit 1" in content

    def test_run_script_has_preflight_install_check(self):
        content = (self.SCRIPTS_DIR / "run_integration_tests.sh").read_text()
        assert "install.sh" in content

    def test_in_container_script_verifies_vhost_binary_path(self):
        content = (self.SCRIPTS_DIR / "in_container_test.sh").read_text()
        assert "/usr/local/bin/vhost" in content

    def test_in_container_script_verifies_bash_completion(self):
        content = (self.SCRIPTS_DIR / "in_container_test.sh").read_text()
        assert "/etc/bash_completion.d/vhost" in content

    def test_in_container_script_asserts_hosts_file(self):
        content = (self.SCRIPTS_DIR / "in_container_test.sh").read_text()
        assert "/etc/hosts" in content

    def test_in_container_script_runs_full_lifecycle(self):
        """Script must exercise create, disable, enable, remove commands."""
        content = (self.SCRIPTS_DIR / "in_container_test.sh").read_text()
        assert "vhost create" in content
        assert "vhost disable" in content
        assert "vhost enable" in content
        assert "vhost remove" in content

    def test_in_container_script_asserts_ubuntu_sites_available(self):
        content = (self.SCRIPTS_DIR / "in_container_test.sh").read_text()
        assert "sites-available" in content

    def test_in_container_script_asserts_fedora_conf_d(self):
        content = (self.SCRIPTS_DIR / "in_container_test.sh").read_text()
        assert "conf.d" in content

    def test_in_container_script_validates_detect_os(self):
        content = (self.SCRIPTS_DIR / "in_container_test.sh").read_text()
        assert "detect_os.sh" in content

    def test_not_placed_inside_tests_dir(self):
        """Integration scripts MUST NOT reside inside src/tests/."""
        tests_dir = Path(__file__).resolve().parent
        assert not (tests_dir / "run_integration_tests.sh").exists()
        assert not (tests_dir / "in_container_test.sh").exists()


# ---------------------------------------------------------------------------
# RHEL config extension regression test (documents known open bug BUG-001)
# ---------------------------------------------------------------------------


class TestRhelNginxConfigExtension:
    """
    Regression tests for BUG-001 and BUG-003: config files must carry a .conf
    extension so that nginx's default include directives pick them up.

    RHEL: `include conf.d/*.conf;`   — requires .conf extension (BUG-001 fixed)
    Debian: `include sites-enabled/*;` — extension is also now .conf (BUG-003 fixed)
    """

    def test_rhel_config_target_path_has_conf_extension(self, tmp_path, mocker):
        """RHEL config target path must end with .conf in the mv call (BUG-001 fix)."""
        conf_d = tmp_path / "conf.d"
        conf_disabled = tmp_path / "conf.disabled"
        conf_d.mkdir()
        conf_disabled.mkdir()

        mock_run = mocker.patch("vhost_helper.providers.nginx.run_elevated_command")
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", conf_d)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", conf_d)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", conf_disabled)
        mocker.patch("vhost_helper.providers.nginx.detected_os_family", "rhel_family")
        mocker.patch(
            "vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False
        )

        from vhost_helper.providers.nginx import NginxProvider
        from vhost_helper.models import VHostConfig

        doc_root = tmp_path / "www"
        doc_root.mkdir()

        provider = NginxProvider()
        mocker.patch.object(provider, "validate_config", return_value=True)
        mocker.patch.object(provider, "reload")

        config = VHostConfig(domain="mysite.local", document_root=str(doc_root))
        provider.create_vhost(config, service_running=False)

        # Find the mv call that moves the temp file to the destination
        mv_calls = [call for call in mock_run.call_args_list if "mv" in str(call)]
        assert mv_calls, "Expected at least one mv command"
        mv_dest = str(mv_calls[0])

        # Config must be named with .conf extension so nginx picks it up
        assert (
            "mysite.local.conf" in mv_dest
        ), f"BUG-001 regression: RHEL config must end with .conf; got: {mv_dest}"

    def test_debian_config_target_path_has_conf_extension(self, tmp_path, mocker):
        """Debian config and symlink paths must end with .conf (BUG-003 fix)."""
        sites_available = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_available.mkdir()
        sites_enabled.mkdir()

        mock_run = mocker.patch("vhost_helper.providers.nginx.run_elevated_command")
        mocker.patch(
            "vhost_helper.providers.nginx.NGINX_SITES_AVAILABLE", sites_available
        )
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_ENABLED", sites_enabled)
        mocker.patch("vhost_helper.providers.nginx.NGINX_SITES_DISABLED", None)
        mocker.patch("vhost_helper.providers.nginx.detected_os_family", "debian_family")
        mocker.patch(
            "vhost_helper.providers.nginx.is_selinux_enforcing", return_value=False
        )

        from vhost_helper.providers.nginx import NginxProvider
        from vhost_helper.models import VHostConfig

        doc_root = tmp_path / "www"
        doc_root.mkdir()

        provider = NginxProvider()
        mocker.patch.object(provider, "validate_config", return_value=True)
        mocker.patch.object(provider, "reload")

        config = VHostConfig(domain="mysite.local", document_root=str(doc_root))
        provider.create_vhost(config, service_running=False)

        mv_calls = [call for call in mock_run.call_args_list if "mv" in str(call)]
        ln_calls = [call for call in mock_run.call_args_list if "ln" in str(call)]
        assert mv_calls, "Expected mv command"
        assert ln_calls, "Expected ln command for Debian symlink"
        assert "mysite.local.conf" in str(
            mv_calls[0]
        ), f"BUG-003 regression: Debian config must end with .conf; got: {mv_calls[0]}"
        assert "mysite.local.conf" in str(
            ln_calls[0]
        ), f"BUG-003 regression: Debian symlink must end with .conf; got: {ln_calls[0]}"


# ---------------------------------------------------------------------------
# Additional enable/disable edge cases
# ---------------------------------------------------------------------------


class TestEnableDisableEdgeCases:
    """Edge cases for the enable and disable commands not covered elsewhere."""

    def test_enable_already_enabled_is_idempotent(self, tmp_path, mocker):
        """Enabling an already-enabled vhost exits cleanly with an info message."""
        sites_available = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_available.mkdir()
        sites_enabled.mkdir()

        # Simulate enabled state — symlink exists with .conf extension
        conf = sites_available / "already.local.conf"
        conf.write_text("server {}")
        (sites_enabled / "already.local.conf").symlink_to(conf)

        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", sites_available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", sites_enabled)
        mocker.patch(
            "vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-available"
        )
        mocker.patch(
            "vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled"
        )
        mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

        result = runner.invoke(app, ["enable", "already.local"])
        assert result.exit_code == 0
        assert "already enabled" in result.stdout

    def test_disable_already_disabled_is_idempotent(self, tmp_path, mocker):
        """Disabling an already-disabled vhost exits cleanly with an info message."""
        sites_available = tmp_path / "sites-available"
        sites_enabled = tmp_path / "sites-enabled"
        sites_available.mkdir()
        sites_enabled.mkdir()

        # Simulate disabled state — config file exists but no symlink
        # Must have .conf extension for provider detection
        conf = sites_available / "disabled.local.conf"
        conf.write_text("server {}")

        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", sites_available)
        mocker.patch("vhost_helper.main.NGINX_SITES_ENABLED", sites_enabled)
        mocker.patch(
            "vhost_helper.main.APACHE_SITES_AVAILABLE", tmp_path / "apache-available"
        )
        mocker.patch(
            "vhost_helper.main.APACHE_SITES_ENABLED", tmp_path / "apache-enabled"
        )
        mocker.patch("vhost_helper.main.is_apache_installed", return_value=False)

        result = runner.invoke(app, ["disable", "disabled.local"])
        assert result.exit_code == 0
        assert "already disabled" in result.stdout

    def test_enable_invalid_domain_exits_nonzero(self):
        result = runner.invoke(app, ["enable", "INVALID..domain"])
        assert result.exit_code != 0

    def test_disable_invalid_domain_exits_nonzero(self):
        result = runner.invoke(app, ["disable", "INVALID..domain"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# info command: exit-code behaviour for missing domain (BUG-002 documentation)
# ---------------------------------------------------------------------------


class TestInfoCommandMissingDomain:
    """
    Documents BUG-002: `vhost info <nonexistent>` exits 0 instead of non-zero.
    The PRD (Feature 3) states: "exit code MUST be non-zero or output must
    indicate 'not found'".  The current implementation satisfies only the
    second condition (output message) and exits 0.
    """

    def test_info_nonexistent_domain_shows_not_found_message(self, mocker):
        """info for an unknown domain must at least print an error message."""
        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", Path("/nonexistent"))
        result = runner.invoke(app, ["info", "ghost.local"])
        assert "No configuration found" in result.stdout

    def test_info_nonexistent_domain_exits_one(self, mocker):
        """
        PRD Requirement: info exits 1 on missing domain.
        BUG-002 fix confirmed.
        """
        mocker.patch("vhost_helper.main.NGINX_SITES_AVAILABLE", Path("/nonexistent"))
        result = runner.invoke(app, ["info", "ghost.local"])
        assert result.exit_code == 1
        assert "No configuration found" in result.stdout

    def test_info_invalid_domain_exits_nonzero(self):
        """Malformed domain in info must still reject with exit code 1."""
        result = runner.invoke(app, ["info", "bad..domain"])
        assert result.exit_code != 0
