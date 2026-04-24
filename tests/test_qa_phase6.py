from pathlib import Path


def get_project_roots():
    """Find both the AI project root and the deliverable root."""
    current = Path(".").resolve()

    # If we are in src/tests, deliverable root is parent
    if current.name == "tests":
        deliverable_root = current.parent
    # If we are in src/, we are at deliverable root
    elif (current / "bin").exists() and (current / "lib").exists():
        deliverable_root = current
    # If we are in the parent, deliverable root is src/
    elif (current / "src").exists():
        deliverable_root = current / "src"
    else:
        deliverable_root = current

    ai_root = deliverable_root.parent
    return ai_root, deliverable_root


def test_two_root_layout_integrity():
    """Verify the physical separation between internal orchestration and public deliverables."""
    ai_root, deliverable_root = get_project_roots()

    # AI Project Root (Internal Orchestration)
    assert (ai_root / "PRD.md").exists()
    assert (ai_root / "ARCHITECTURE.md").exists()
    assert (ai_root / "project_manifest.md").exists()
    assert (ai_root / "DESIGN_SPEC.md").exists()
    assert (ai_root / "SECURITY_AUDIT.md").exists()
    assert (ai_root / "QA_REPORT.md").exists()
    assert (ai_root / "global_context.md").exists()
    assert (ai_root / "API_DOCS.md").exists()

    # Deliverable Root (Public Distribution)
    assert (deliverable_root / "README.md").exists()
    assert (deliverable_root / "install.sh").exists()
    assert (deliverable_root / "uninstall.sh").exists()
    assert (deliverable_root / "requirements.txt").exists()
    assert (deliverable_root / "bin" / "vhost").exists()
    assert (deliverable_root / "lib" / "vhost_helper").exists()

    # Privacy check: internal files MUST NOT be in deliverable root
    internal_files = [
        "PRD.md",
        "ARCHITECTURE.md",
        "project_manifest.md",
        "DESIGN_SPEC.md",
        "SECURITY_AUDIT.md",
    ]
    for f in internal_files:
        assert not (
            deliverable_root / f
        ).exists(), f"Internal file {f} found in deliverable root!"


def test_ci_config_coverage_requirements():
    """Verify CI configuration has the correct coverage thresholds and paths."""
    _, deliverable_root = get_project_roots()
    ci_file = deliverable_root / ".github" / "workflows" / "ci.yml"

    assert ci_file.exists(), f"CI configuration file not found at {ci_file}."
    content = ci_file.read_text()

    assert "--cov=lib/vhost_helper" in content
    assert "--cov-report=term-missing" in content
    assert "--cov-fail-under=80" in content
    assert "push:" in content
    assert "pull_request:" in content

    # Ensure no legacy 'src/' prefixes in CI
    assert "src/requirements.txt" not in content
    assert "src/lib" not in content


def test_install_script_paths():
    """Verify install.sh uses relative paths correctly within the deliverable root."""
    _, deliverable_root = get_project_roots()
    install_script = deliverable_root / "install.sh"
    content = install_script.read_text()

    # It should use its own location to find bin/vhost
    assert 'VHOST_BIN="$SOURCE_DIR/bin/vhost"' in content
    # It should find requirements.txt in the same dir
    assert "requirements.txt" in content


def test_gitignore_enforcement():
    """Verify .gitignore excludes internal orchestration files."""
    _, deliverable_root = get_project_roots()
    gitignore = deliverable_root / ".gitignore"
    assert gitignore.exists()
    content = gitignore.read_text()

    assert "PRD.md" in content
    assert "ARCHITECTURE.md" in content
    assert "project_manifest.md" in content
    assert "GEMINI.md" in content
