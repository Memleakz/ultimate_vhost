"""
Unit tests for log path extraction functions in ``vhost_helper.logs``.

All tests are pure — no filesystem or subprocess access.
"""

from vhost_helper.logs import extract_nginx_log_paths, extract_apache_log_paths

# ---------------------------------------------------------------------------
# extract_nginx_log_paths
# ---------------------------------------------------------------------------


class TestExtractNginxLogPaths:
    def test_both_paths_present(self):
        config = (
            "server {\n"
            "    access_log /var/log/nginx/myapp.test.access.log;\n"
            "    error_log /var/log/nginx/myapp.test.error.log;\n"
            "}\n"
        )
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/myapp.test.access.log"
        assert error == "/var/log/nginx/myapp.test.error.log"

    def test_access_log_off_returns_none(self):
        config = (
            "server {\n"
            "    access_log off;\n"
            "    error_log /var/log/nginx/myapp.error.log;\n"
            "}\n"
        )
        access, error = extract_nginx_log_paths(config)
        assert access is None
        assert error == "/var/log/nginx/myapp.error.log"

    def test_error_log_off_returns_none(self):
        config = (
            "server {\n"
            "    access_log /var/log/nginx/myapp.access.log;\n"
            "    error_log off;\n"
            "}\n"
        )
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/myapp.access.log"
        assert error is None

    def test_both_off_returns_none_tuple(self):
        config = "server {\n    access_log off;\n    error_log off;\n}\n"
        access, error = extract_nginx_log_paths(config)
        assert access is None
        assert error is None

    def test_error_log_directive_missing(self):
        config = "server {\n    access_log /var/log/nginx/myapp.access.log;\n}\n"
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/myapp.access.log"
        assert error is None

    def test_access_log_directive_missing(self):
        config = "server {\n    error_log /var/log/nginx/myapp.error.log;\n}\n"
        access, error = extract_nginx_log_paths(config)
        assert access is None
        assert error == "/var/log/nginx/myapp.error.log"

    def test_empty_config_returns_none_tuple(self):
        access, error = extract_nginx_log_paths("")
        assert access is None
        assert error is None

    def test_no_log_directives_returns_none_tuple(self):
        config = "server {\n    listen 80;\n    server_name example.com;\n}\n"
        access, error = extract_nginx_log_paths(config)
        assert access is None
        assert error is None

    def test_inline_comment_stripped(self):
        config = (
            "server {\n"
            "    error_log /var/log/nginx/err.log; # main log\n"
            "    access_log /var/log/nginx/acc.log; # access\n"
            "}\n"
        )
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/acc.log"
        assert error == "/var/log/nginx/err.log"

    def test_inline_comment_on_access_log_line(self):
        config = "    access_log /var/log/nginx/myapp.access.log combined; # prod\n"
        access, _error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/myapp.access.log"

    def test_only_first_occurrence_used(self):
        config = (
            "    access_log /var/log/nginx/first.log;\n"
            "    access_log /var/log/nginx/second.log;\n"
        )
        access, _error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/first.log"

    def test_case_insensitive_directive_match(self):
        config = (
            "    ACCESS_LOG /var/log/nginx/access.log;\n"
            "    ERROR_LOG /var/log/nginx/error.log;\n"
        )
        access, error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/access.log"
        assert error == "/var/log/nginx/error.log"

    def test_path_with_log_format_token(self):
        """Nginx supports: access_log /path/to/file combined;"""
        config = "    access_log /var/log/nginx/myapp.access.log combined;\n"
        access, _error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/myapp.access.log"

    def test_path_without_trailing_semicolon(self):
        config = "    access_log /var/log/nginx/nocolon.log\n"
        access, _error = extract_nginx_log_paths(config)
        assert access == "/var/log/nginx/nocolon.log"


# ---------------------------------------------------------------------------
# extract_apache_log_paths
# ---------------------------------------------------------------------------


class TestExtractApacheLogPaths:
    def test_both_paths_present(self):
        config = (
            "<VirtualHost *:80>\n"
            "    CustomLog /var/log/apache2/myapp.test.access.log combined\n"
            "    ErrorLog /var/log/apache2/myapp.test.error.log\n"
            "</VirtualHost>\n"
        )
        access, error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/myapp.test.access.log"
        assert error == "/var/log/apache2/myapp.test.error.log"

    def test_custom_log_format_token_ignored(self):
        """The log-format string after the path MUST be ignored."""
        config = "    CustomLog /var/log/apache2/myapp.access.log combined\n"
        access, _error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/myapp.access.log"

    def test_case_insensitive_customlog(self):
        config = "    customlog /var/log/apache2/lower.log combined\n"
        access, _error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/lower.log"

    def test_case_insensitive_errorlog(self):
        config = "    errorlog /var/log/apache2/lower.error.log\n"
        _access, error = extract_apache_log_paths(config)
        assert error == "/var/log/apache2/lower.error.log"

    def test_uppercase_directives(self):
        config = (
            "    CUSTOMLOG /var/log/apache2/upper.access.log combined\n"
            "    ERRORLOG /var/log/apache2/upper.error.log\n"
        )
        access, error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/upper.access.log"
        assert error == "/var/log/apache2/upper.error.log"

    def test_inline_comment_stripped(self):
        config = (
            "    CustomLog /var/log/apache2/myapp.access.log combined # prod\n"
            "    ErrorLog /var/log/apache2/myapp.error.log # errors\n"
        )
        access, error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/myapp.access.log"
        assert error == "/var/log/apache2/myapp.error.log"

    def test_empty_config_returns_none_tuple(self):
        access, error = extract_apache_log_paths("")
        assert access is None
        assert error is None

    def test_no_log_directives_returns_none_tuple(self):
        config = "<VirtualHost *:80>\n    DocumentRoot /var/www/html\n</VirtualHost>\n"
        access, error = extract_apache_log_paths(config)
        assert access is None
        assert error is None

    def test_errorlog_missing_returns_none(self):
        config = "    CustomLog /var/log/apache2/only.access.log combined\n"
        access, error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/only.access.log"
        assert error is None

    def test_customlog_missing_returns_none(self):
        config = "    ErrorLog /var/log/apache2/only.error.log\n"
        access, error = extract_apache_log_paths(config)
        assert access is None
        assert error == "/var/log/apache2/only.error.log"

    def test_only_first_occurrence_used(self):
        config = (
            "    CustomLog /var/log/apache2/first.log combined\n"
            "    CustomLog /var/log/apache2/second.log combined\n"
        )
        access, _error = extract_apache_log_paths(config)
        assert access == "/var/log/apache2/first.log"

    def test_rhel_style_paths(self):
        config = (
            "    CustomLog /var/log/httpd/myapp.access.log combined\n"
            "    ErrorLog /var/log/httpd/myapp.error.log\n"
        )
        access, error = extract_apache_log_paths(config)
        assert access == "/var/log/httpd/myapp.access.log"
        assert error == "/var/log/httpd/myapp.error.log"
