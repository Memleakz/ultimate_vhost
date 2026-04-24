"""
Test suite for template_inspector.py — Dynamic Template Variable Discovery.

Covers:
  - extract_variables: filters, conditionals, loops, macros, advanced syntax
  - extract_metadata:  presence/absence/partial metadata blocks
  - list_templates:    filesystem scan and provider filtering
  - resolve_template_path: valid/invalid name conventions
  - CLI integration:   `vhost templates list` and `vhost templates inspect`
"""

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vhost_helper.template_inspector import (
    extract_metadata,
    extract_variables,
    list_templates,
    resolve_template_path,
)
from vhost_helper.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, filename: str, content: str) -> Path:
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


FULL_METADATA = """\
{# ---
variables:
  - name: domain
    description: "The fully-qualified domain name for the virtual host."
  - name: port
    description: "HTTP port the server block listens on."
    default: "80"
  - name: document_root
    description: "Absolute path to the site web root."
--- #}
"""


# ---------------------------------------------------------------------------
# extract_variables
# ---------------------------------------------------------------------------

class TestExtractVariables:
    def test_simple_variable(self, tmp_path):
        t = _write(tmp_path, "t.j2", "Hello {{ name }}!")
        assert extract_variables(t) == ["name"]

    def test_multiple_variables_sorted(self, tmp_path):
        t = _write(tmp_path, "t.j2", "{{ z }} {{ a }} {{ m }}")
        assert extract_variables(t) == ["a", "m", "z"]

    def test_filter_not_counted_as_variable(self, tmp_path):
        t = _write(tmp_path, "t.j2", "{{ domain | lower }}")
        assert extract_variables(t) == ["domain"]

    def test_loop_builtin_excluded(self, tmp_path):
        t = _write(tmp_path, "t.j2", "{% for item in items %}{{ loop.index }}{% endfor %}")
        assert "loop" not in extract_variables(t)
        assert "item" not in extract_variables(t)
        assert "items" in extract_variables(t)

    def test_variables_inside_conditional(self, tmp_path):
        t = _write(tmp_path, "t.j2", "{% if runtime == 'php' %}{{ php_socket }}{% endif %}")
        vars_ = extract_variables(t)
        assert "runtime" in vars_
        assert "php_socket" in vars_

    def test_variables_inside_for_loop(self, tmp_path):
        t = _write(tmp_path, "t.j2", "{% for h in hosts %}{{ h.name }}{% endfor %}")
        assert "hosts" in extract_variables(t)

    def test_variables_inside_macro(self, tmp_path):
        t = _write(tmp_path, "t.j2", """\
            {% macro render(item) %}{{ item.value }}{% endmacro %}
            {{ render(my_item) }}
        """)
        assert "my_item" in extract_variables(t)

    def test_advanced_syntax_no_exception(self, tmp_path):
        """Namespace, call blocks, and whitespace control must not raise."""
        t = _write(tmp_path, "t.j2", """\
            {%- set ns = namespace(count=0) -%}
            {%- for x in items -%}
              {%- set ns.count = ns.count + 1 -%}
            {%- endfor -%}
            {{ ns.count }}
        """)
        result = extract_variables(t)
        assert isinstance(result, list)

    def test_empty_template_returns_empty_list(self, tmp_path):
        t = _write(tmp_path, "t.j2", "")
        assert extract_variables(t) == []

    def test_comment_only_template_returns_empty_list(self, tmp_path):
        t = _write(tmp_path, "t.j2", "{# This is a comment only #}")
        assert extract_variables(t) == []

    def test_nonexistent_file_returns_empty_list(self, tmp_path):
        missing = tmp_path / "missing.j2"
        assert extract_variables(missing) == []

    @pytest.mark.parametrize("template_path", list(
        (Path(__file__).parent.parent / "templates").glob("**/*.conf.j2")
    ))
    def test_all_existing_templates_no_exception(self, template_path):
        """extract_variables must not raise for any .j2 file in the codebase."""
        result = extract_variables(template_path)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# extract_metadata
# ---------------------------------------------------------------------------

class TestExtractMetadata:
    def test_valid_full_metadata_block(self, tmp_path):
        t = _write(tmp_path, "t.j2", FULL_METADATA + "{{ domain }}")
        meta = extract_metadata(t)
        assert "domain" in meta
        assert meta["domain"]["description"] == "The fully-qualified domain name for the virtual host."
        assert meta["domain"]["default"] is None
        assert meta["port"]["default"] == "80"

    def test_missing_metadata_block_returns_empty_dict(self, tmp_path):
        t = _write(tmp_path, "t.j2", "{{ domain }}")
        assert extract_metadata(t) == {}

    def test_partial_metadata_only_some_variables(self, tmp_path):
        content = """\
            {# ---
            variables:
              - name: domain
                description: "The domain."
            --- #}
            {{ domain }} {{ port }}
        """
        t = _write(tmp_path, "t.j2", content)
        meta = extract_metadata(t)
        assert "domain" in meta
        assert "port" not in meta

    def test_default_value_parsed_correctly(self, tmp_path):
        t = _write(tmp_path, "t.j2", FULL_METADATA)
        meta = extract_metadata(t)
        assert meta["port"]["default"] == "80"

    def test_missing_default_is_none(self, tmp_path):
        t = _write(tmp_path, "t.j2", FULL_METADATA)
        meta = extract_metadata(t)
        assert meta["domain"]["default"] is None

    def test_malformed_yaml_returns_empty_dict(self, tmp_path):
        content = "{# ---\n: : : invalid yaml : : :\n--- #}\n{{ domain }}"
        t = _write(tmp_path, "t.j2", content)
        assert extract_metadata(t) == {}

    def test_empty_variables_list_returns_empty_dict(self, tmp_path):
        content = "{# ---\nvariables: []\n--- #}\n{{ domain }}"
        t = _write(tmp_path, "t.j2", content)
        assert extract_metadata(t) == {}

    def test_nonexistent_file_returns_empty_dict(self, tmp_path):
        missing = tmp_path / "missing.j2"
        assert extract_metadata(missing) == {}


# ---------------------------------------------------------------------------
# list_templates
# ---------------------------------------------------------------------------

class TestListTemplates:
    def _make_tree(self, tmp_path: Path) -> Path:
        (tmp_path / "nginx").mkdir()
        (tmp_path / "nginx" / "static.conf.j2").write_text("")
        (tmp_path / "nginx" / "php.conf.j2").write_text("")
        (tmp_path / "apache").mkdir()
        (tmp_path / "apache" / "static.conf.j2").write_text("")
        (tmp_path / "apache" / "php-fpm.conf.j2").write_text("")
        return tmp_path

    def test_lists_all_providers(self, tmp_path):
        d = self._make_tree(tmp_path)
        result = list_templates(d)
        assert set(result.keys()) == {"nginx", "apache"}

    def test_filter_by_nginx(self, tmp_path):
        d = self._make_tree(tmp_path)
        result = list_templates(d, provider="nginx")
        assert list(result.keys()) == ["nginx"]
        assert "apache" not in result

    def test_filter_by_apache(self, tmp_path):
        d = self._make_tree(tmp_path)
        result = list_templates(d, provider="apache")
        assert list(result.keys()) == ["apache"]

    def test_modes_are_sorted(self, tmp_path):
        d = self._make_tree(tmp_path)
        result = list_templates(d)
        assert result["nginx"] == sorted(result["nginx"])

    def test_new_file_auto_discovered(self, tmp_path):
        d = self._make_tree(tmp_path)
        (d / "nginx" / "grpc-proxy.conf.j2").write_text("")
        result = list_templates(d, provider="nginx")
        assert "grpc-proxy" in result["nginx"]

    def test_unknown_provider_returns_empty_dict(self, tmp_path):
        d = self._make_tree(tmp_path)
        result = list_templates(d, provider="iis")
        assert result == {}

    def test_non_j2_files_excluded(self, tmp_path):
        d = self._make_tree(tmp_path)
        (d / "nginx" / "README.md").write_text("")
        (d / "nginx" / "backup.conf").write_text("")
        result = list_templates(d, provider="nginx")
        assert "README" not in result["nginx"]
        assert "backup" not in result["nginx"]

    def test_nonexistent_templates_dir_returns_empty(self, tmp_path):
        result = list_templates(tmp_path / "does_not_exist")
        assert result == {}

    def test_discovery_count_matches_filesystem(self):
        """CLI list output count must equal filesystem .conf.j2 count."""
        templates_dir = Path(__file__).parent.parent / "templates"
        result = list_templates(templates_dir)
        fs_count = sum(len(v) for v in result.values())
        glob_count = len(list(templates_dir.glob("**/*.conf.j2")))
        assert fs_count == glob_count


# ---------------------------------------------------------------------------
# resolve_template_path
# ---------------------------------------------------------------------------

class TestResolveTemplatePath:
    def _make_tree(self, tmp_path: Path) -> Path:
        (tmp_path / "nginx").mkdir()
        (tmp_path / "nginx" / "php.conf.j2").write_text("")
        (tmp_path / "apache").mkdir()
        (tmp_path / "apache" / "php-fpm.conf.j2").write_text("")
        (tmp_path / "apache" / "python-proxy.conf.j2").write_text("")
        return tmp_path

    def test_simple_name_resolves(self, tmp_path):
        d = self._make_tree(tmp_path)
        path = resolve_template_path("nginx-php", d)
        assert path is not None
        assert path.name == "php.conf.j2"

    def test_multi_word_mode_resolves(self, tmp_path):
        """apache-php-fpm → apache/php-fpm.conf.j2"""
        d = self._make_tree(tmp_path)
        path = resolve_template_path("apache-php-fpm", d)
        assert path is not None
        assert path.name == "php-fpm.conf.j2"

    def test_three_part_mode_resolves(self, tmp_path):
        """apache-python-proxy → apache/python-proxy.conf.j2"""
        d = self._make_tree(tmp_path)
        path = resolve_template_path("apache-python-proxy", d)
        assert path is not None
        assert path.name == "python-proxy.conf.j2"

    def test_unknown_name_returns_none(self, tmp_path):
        d = self._make_tree(tmp_path)
        assert resolve_template_path("nginx-does-not-exist", d) is None

    def test_no_hyphen_returns_none(self, tmp_path):
        d = self._make_tree(tmp_path)
        assert resolve_template_path("nohyphen", d) is None

    def test_empty_string_returns_none(self, tmp_path):
        d = self._make_tree(tmp_path)
        assert resolve_template_path("", d) is None


# ---------------------------------------------------------------------------
# CLI Integration — `vhost templates list`
# ---------------------------------------------------------------------------

class TestTemplatesListCommand:
    def test_list_all_providers_exits_zero(self):
        result = runner.invoke(app, ["templates", "list"])
        assert result.exit_code == 0

    def test_list_includes_nginx(self):
        result = runner.invoke(app, ["templates", "list"])
        assert "nginx" in result.output.lower()

    def test_list_includes_apache(self):
        result = runner.invoke(app, ["templates", "list"])
        assert "apache" in result.output.lower()

    def test_list_filter_apache_excludes_nginx(self):
        result = runner.invoke(app, ["templates", "list", "--provider", "apache"])
        assert result.exit_code == 0
        assert "nginx" not in result.output.lower()

    def test_list_filter_nginx_excludes_apache(self):
        result = runner.invoke(app, ["templates", "list", "--provider", "nginx"])
        assert result.exit_code == 0
        assert "apache" not in result.output.lower()

    def test_list_unknown_provider_exits_nonzero(self):
        result = runner.invoke(app, ["templates", "list", "--provider", "iis"])
        assert result.exit_code != 0

    def test_list_output_not_empty(self):
        result = runner.invoke(app, ["templates", "list"])
        assert len(result.output.strip()) > 0


# ---------------------------------------------------------------------------
# CLI Integration — `vhost templates inspect`
# ---------------------------------------------------------------------------

class TestTemplatesInspectCommand:
    def test_inspect_nginx_php_exits_zero(self):
        result = runner.invoke(app, ["templates", "inspect", "nginx-php"])
        assert result.exit_code == 0

    def test_inspect_nginx_php_includes_domain(self):
        result = runner.invoke(app, ["templates", "inspect", "nginx-php"])
        assert "domain" in result.output

    def test_inspect_nginx_php_includes_document_root(self):
        result = runner.invoke(app, ["templates", "inspect", "nginx-php"])
        assert "document_root" in result.output

    def test_inspect_nginx_php_includes_php_socket(self):
        result = runner.invoke(app, ["templates", "inspect", "nginx-php"])
        assert "php_socket" in result.output

    def test_inspect_apache_static_exits_zero(self):
        result = runner.invoke(app, ["templates", "inspect", "apache-static"])
        assert result.exit_code == 0

    def test_inspect_apache_static_includes_domain(self):
        result = runner.invoke(app, ["templates", "inspect", "apache-static"])
        assert "domain" in result.output

    def test_inspect_unknown_template_exits_1(self):
        result = runner.invoke(app, ["templates", "inspect", "does-not-exist"])
        assert result.exit_code == 1

    def test_inspect_unknown_template_error_message(self):
        result = runner.invoke(app, ["templates", "inspect", "does-not-exist"])
        assert "does-not-exist" in result.output

    def test_inspect_shows_description_column(self):
        result = runner.invoke(app, ["templates", "inspect", "nginx-static"])
        assert "description" in result.output.lower() or "Description" in result.output

    def test_inspect_shows_default_column(self):
        result = runner.invoke(app, ["templates", "inspect", "nginx-static"])
        assert "default" in result.output.lower() or "Default" in result.output

    def test_inspect_em_dash_for_no_default(self):
        result = runner.invoke(app, ["templates", "inspect", "nginx-static"])
        assert "—" in result.output
