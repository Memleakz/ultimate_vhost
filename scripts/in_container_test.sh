#!/bin/bash
# In-container integration test runner for VHost Helper.
# Executed inside an ephemeral Docker container by run_integration_tests.sh.
#
# Usage: bash in_container_test.sh <ubuntu|fedora> <nginx|apache>
#
# Exit codes:
#   0 — all assertions passed
#   1 — one or more assertions failed

set -uo pipefail

DISTRO="${1:-unknown}"
PROVIDER="${2:-nginx}"
DOMAIN="testsite.local"
DOC_ROOT="/tmp/testsite"
SRC_DIR="/opt/vhost-src"
INSTALL_DIR="/opt/vhost-helper"

PASS_COUNT=0
FAIL_COUNT=0
FIRST_FAILURE=""

# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    echo "  [PASS] $1"
}

fail() {
    local msg="$1"
    local expected="${2:-}"
    local actual="${3:-}"

    FAIL_COUNT=$((FAIL_COUNT + 1))
    if [ -z "$FIRST_FAILURE" ]; then
        FIRST_FAILURE="$msg"
        [ -n "$expected" ] && FIRST_FAILURE="$msg (expected: $expected | actual: $actual)"
    fi

    if [ -n "$expected" ]; then
        echo "  [FAIL] $msg — expected: $expected | actual: $actual"
    else
        echo "  [FAIL] $msg"
    fi
}

# Abort immediately on a critical setup failure (no point running the rest).
critical_fail() {
    echo ""
    echo "  [CRITICAL] $1 — aborting test run."
    FAIL_COUNT=$((FAIL_COUNT + 1))
    [ -z "$FIRST_FAILURE" ] && FIRST_FAILURE="$1"
    print_summary
    exit 1
}

print_summary() {
    echo ""
    echo "============================================================"
    echo "  Distribution : $DISTRO"
    echo "  Provider     : $PROVIDER"
    echo "  Passed       : $PASS_COUNT"
    echo "  Failed       : $FAIL_COUNT"
    if [ "$FAIL_COUNT" -eq 0 ]; then
        echo "  Result       : [PASS]"
    else
        echo "  Result       : [FAIL]"
        [ -n "$FIRST_FAILURE" ] && echo "  First failure: $FIRST_FAILURE"
    fi
    echo "============================================================"
}

# --------------------------------------------------------------------------
# Assertion helpers
# --------------------------------------------------------------------------

assert_cmd_succeeds() {
    local msg="$1"
    shift
    if "$@" 2>&1; then
        pass "$msg"
    else
        fail "$msg" "exit 0" "exit $?"
    fi
}

assert_file_exists() {
    if [ -e "$2" ]; then
        pass "$1"
    else
        fail "$1" "exists: $2" "not found"
    fi
}

assert_file_not_exists() {
    if [ ! -e "$2" ]; then
        pass "$1"
    else
        fail "$1" "not found: $2" "file exists"
    fi
}

assert_symlink_exists() {
    if [ -L "$2" ]; then
        pass "$1"
    else
        fail "$1" "symlink: $2" "not a symlink or missing"
    fi
}

assert_symlink_not_exists() {
    if [ ! -L "$2" ]; then
        pass "$1"
    else
        fail "$1" "symlink absent: $2" "symlink present"
    fi
}

assert_hosts_contains() {
    if grep -qF "$2" /etc/hosts; then
        pass "$1"
    else
        fail "$1" "/etc/hosts contains '$2'" "entry absent"
    fi
}

assert_hosts_not_contains() {
    if ! grep -qF "$2" /etc/hosts; then
        pass "$1"
    else
        fail "$1" "/etc/hosts lacks '$2'" "entry present"
    fi
}

assert_output_contains() {
    local msg="$1"
    local pattern="$2"
    local output="$3"
    if echo "$output" | grep -qiE "$pattern"; then
        pass "$msg"
    else
        fail "$msg" "output matches: $pattern" "$(echo "$output" | head -3)"
    fi
}

assert_output_not_contains() {
    local msg="$1"
    local pattern="$2"
    local output="$3"
    if ! echo "$output" | grep -qiE "$pattern"; then
        pass "$msg"
    else
        fail "$msg" "output does not match: $pattern" "pattern found in output"
    fi
}

# Check that every directory under $3 has mode $2 (e.g. "755").
# Uses -perm -mode (at-least) so directories with SetGID (2755) also pass a 755 check.
assert_all_dirs_mode() {
    local msg="$1"
    local expected_mode="$2"
    local target_dir="$3"
    local bad_dirs
    bad_dirs="$(find "$target_dir" -type d ! -perm -"$expected_mode" 2>/dev/null || true)"
    if [ -z "$bad_dirs" ]; then
        pass "$msg"
    else
        fail "$msg" "all dirs mode (at least) $expected_mode" "offending: $(echo "$bad_dirs" | head -3)"
    fi
}

# Check that every regular file under $3 has mode $2 (e.g. "644").
assert_all_files_mode() {
    local msg="$1"
    local expected_mode="$2"
    local target_dir="$3"
    local bad_files
    bad_files="$(find "$target_dir" -type f ! -perm "$expected_mode" 2>/dev/null || true)"
    if [ -z "$bad_files" ]; then
        pass "$msg"
    else
        fail "$msg" "all files mode $expected_mode" "offending: $(echo "$bad_files" | head -3)"
    fi
}

# Check that every directory under $3 has the SetGID bit set.
assert_all_dirs_setgid() {
    local msg="$1"
    local target_dir="$2"
    local bad_dirs
    bad_dirs="$(find "$target_dir" -type d ! -perm /g+s 2>/dev/null || true)"
    if [ -z "$bad_dirs" ]; then
        pass "$msg"
    else
        fail "$msg" "all dirs have SetGID (g+s)" "offending: $(echo "$bad_dirs" | head -3)"
    fi
}

# Check that the path $3 is owned by user $2.
assert_owner() {
    local msg="$1"
    local expected_owner="$2"
    local target_path="$3"
    local actual_owner
    actual_owner="$(stat -c '%U' "$target_path" 2>/dev/null || echo 'unknown')"
    if [ "$actual_owner" = "$expected_owner" ]; then
        pass "$msg"
    else
        fail "$msg" "owner=$expected_owner" "owner=$actual_owner"
    fi
}

# Check that the path $3 is owned by group $2.
assert_group() {
    local msg="$1"
    local expected_group="$2"
    local target_path="$3"
    local actual_group
    actual_group="$(stat -c '%G' "$target_path" 2>/dev/null || echo 'unknown')"
    if [ "$actual_group" = "$expected_group" ]; then
        pass "$msg"
    else
        fail "$msg" "group=$expected_group" "group=$actual_group"
    fi
}


# --------------------------------------------------------------------------
# STEP 1: Install OS-specific packages
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 1: Installing OS packages (distro=$DISTRO, provider=$PROVIDER) ==="

if [ "$DISTRO" = "ubuntu" ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y -q > /dev/null 2>&1 \
        || critical_fail "apt-get update failed"

    if [ "$PROVIDER" = "apache" ]; then
        apt-get install -y -q apache2 python3 python3-pip python3-venv bash-completion > /dev/null 2>&1 \
            || critical_fail "apt-get install (apache2) failed"
        pass "Ubuntu packages installed (apache2, python3, python3-pip, python3-venv, bash-completion)"
    else
        apt-get install -y -q nginx python3 python3-pip python3-venv bash-completion > /dev/null 2>&1 \
            || critical_fail "apt-get install (nginx) failed"
        pass "Ubuntu packages installed (nginx, python3, python3-pip, python3-venv, bash-completion)"
    fi

elif [ "$DISTRO" = "fedora" ]; then
    if [ "$PROVIDER" = "apache" ]; then
        dnf install -y -q httpd python3 python3-pip bash-completion > /dev/null 2>&1 \
            || critical_fail "dnf install (httpd) failed"
        pass "Fedora packages installed (httpd, python3, python3-pip, bash-completion)"
    else
        dnf install -y -q nginx python3 python3-pip bash-completion > /dev/null 2>&1 \
            || critical_fail "dnf install (nginx) failed"
        pass "Fedora packages installed (nginx, python3, python3-pip, bash-completion)"
    fi
else
    critical_fail "Unsupported distribution: '$DISTRO'"
fi

# Ensure web server binaries are in PATH for Python's shutil.which().
export PATH="$PATH:/usr/sbin:/sbin"

# --------------------------------------------------------------------------
# STEP 2: Run install.sh
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 2: Running install.sh ==="

if bash "$SRC_DIR/install.sh"; then
    pass "install.sh completed with exit code 0"
else
    critical_fail "install.sh failed — cannot proceed without a working installation"
fi

# --------------------------------------------------------------------------
# STEP 3: Verify installation
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 3: Verifying installation ==="

VHOST_BIN="$(command -v vhost 2>/dev/null || true)"
if [ "$VHOST_BIN" = "/usr/local/bin/vhost" ]; then
    pass "vhost binary accessible at /usr/local/bin/vhost"
else
    fail "vhost binary accessible at /usr/local/bin/vhost" \
         "/usr/local/bin/vhost" "${VHOST_BIN:-not found}"
fi

assert_file_exists \
    "Bash completion registered at /etc/bash_completion.d/vhost" \
    "/etc/bash_completion.d/vhost"

if /opt/vhost-helper/.venv/bin/python3 -c "import typer; import pydantic; import jinja2; import rich" 2>/dev/null; then
    pass "Required Python dependencies are importable (typer, pydantic, jinja2, rich)"
else
    fail "Required Python dependencies are importable (typer, pydantic, jinja2, rich)"
fi

# --------------------------------------------------------------------------
# STEP 4: Verify OS detection
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 4: Verifying OS detection (detect_os.sh) ==="

DETECT_OUT="$(bash "$INSTALL_DIR/bin/detect_os.sh" 2>&1 || true)"
assert_output_contains \
    "detect_os.sh identifies the distribution as '$DISTRO'" \
    "$DISTRO" \
    "$DETECT_OUT"

# --------------------------------------------------------------------------
# STEP 5: Create virtual host
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 5: Creating virtual host ($DOMAIN) via provider=$PROVIDER ==="

mkdir -p "$DOC_ROOT"

CREATE_OUTPUT="$(vhost create "$DOMAIN" "$DOC_ROOT" --provider "$PROVIDER" 2>&1)"
CREATE_EXIT=$?

if [ "$CREATE_EXIT" -eq 0 ]; then
    pass "vhost create '$DOMAIN' --provider $PROVIDER exits with code 0"
else
    echo "$CREATE_OUTPUT"
    critical_fail "vhost create '$DOMAIN' --provider $PROVIDER failed (exit $CREATE_EXIT) — cannot test lifecycle"
fi

# Verify /etc/hosts entry is unique (no duplicates).
# Use grep -cE with a word-boundary pattern so 'www.testsite.local' is not
# counted as a second match for 'testsite.local'.
HOSTS_COUNT="$(grep -cE "[[:space:]]${DOMAIN}([[:space:]]|$)" /etc/hosts 2>/dev/null || true)"
if [ "${HOSTS_COUNT:-0}" -eq 1 ]; then
    pass "/etc/hosts contains exactly one entry for '$DOMAIN' (no duplicates)"
else
    fail "/etc/hosts contains exactly one entry for '$DOMAIN'" "1" "${HOSTS_COUNT:-0}"
fi

assert_hosts_contains \
    "/etc/hosts contains '$DOMAIN' after create" \
    "$DOMAIN"

if [ "$PROVIDER" = "nginx" ]; then
    if [ "$DISTRO" = "ubuntu" ]; then
        assert_file_exists \
            "Nginx config exists at /etc/nginx/sites-available/$DOMAIN.conf" \
            "/etc/nginx/sites-available/$DOMAIN.conf"
        assert_symlink_exists \
            "Nginx symlink exists at /etc/nginx/sites-enabled/$DOMAIN.conf" \
            "/etc/nginx/sites-enabled/$DOMAIN.conf"
    elif [ "$DISTRO" = "fedora" ]; then
        assert_file_exists \
            "Nginx config exists at /etc/nginx/conf.d/$DOMAIN.conf" \
            "/etc/nginx/conf.d/$DOMAIN.conf"
    fi
elif [ "$PROVIDER" = "apache" ]; then
    if [ "$DISTRO" = "ubuntu" ]; then
        assert_file_exists \
            "Apache config exists at /etc/apache2/sites-available/$DOMAIN.conf" \
            "/etc/apache2/sites-available/$DOMAIN.conf"
        assert_symlink_exists \
            "Apache symlink exists at /etc/apache2/sites-enabled/$DOMAIN.conf" \
            "/etc/apache2/sites-enabled/$DOMAIN.conf"
    elif [ "$DISTRO" = "fedora" ]; then
        assert_file_exists \
            "Apache config exists at /etc/httpd/conf.d/$DOMAIN.conf" \
            "/etc/httpd/conf.d/$DOMAIN.conf"
    fi
fi

# --------------------------------------------------------------------------
# STEP 5b: Verify webroot permissions (Gold Standard: 755/644/g+s + ownership)
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 5b: Verifying webroot permissions for '$DOC_ROOT' ==="

# Determine expected group based on distro and provider
if [ "$DISTRO" = "ubuntu" ]; then
    EXPECTED_WEB_GROUP="www-data"
elif [ "$DISTRO" = "fedora" ]; then
    if [ "$PROVIDER" = "apache" ]; then
        EXPECTED_WEB_GROUP="apache"
    else
        EXPECTED_WEB_GROUP="nginx"
    fi
else
    EXPECTED_WEB_GROUP="www-data"
fi

# The webroot itself and sub-directories must be owned by the web server group
assert_group \
    "Webroot '$DOC_ROOT' is owned by group '$EXPECTED_WEB_GROUP'" \
    "$EXPECTED_WEB_GROUP" \
    "$DOC_ROOT"

# All directories must be 755
assert_all_dirs_mode \
    "All directories under webroot have mode 755" \
    "755" \
    "$DOC_ROOT"

# All files must be 644 (webroot may be empty — only test if files exist)
if find "$DOC_ROOT" -type f | grep -q .; then
    assert_all_files_mode \
        "All files under webroot have mode 644" \
        "644" \
        "$DOC_ROOT"
else
    pass "No files yet in webroot — file-mode check skipped (webroot is empty)"
fi

# All directories must carry the SetGID bit for group-inheriting new files
assert_all_dirs_setgid \
    "All directories under webroot have SetGID bit (g+s)" \
    "$DOC_ROOT"

# --------------------------------------------------------------------------
# STEP 5c: Verify config-file mode (644) and create idempotency
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 5c: Config-file permissions and duplicate-create guard ==="

# The rendered vhost config must be world-readable but not world-writable (644)
if [ "$PROVIDER" = "nginx" ]; then
    if [ "$DISTRO" = "ubuntu" ]; then
        CONF_PATH="/etc/nginx/sites-available/$DOMAIN.conf"
    else
        CONF_PATH="/etc/nginx/conf.d/$DOMAIN.conf"
    fi
elif [ "$PROVIDER" = "apache" ]; then
    if [ "$DISTRO" = "ubuntu" ]; then
        CONF_PATH="/etc/apache2/sites-available/$DOMAIN.conf"
    else
        CONF_PATH="/etc/httpd/conf.d/$DOMAIN.conf"
    fi
fi

CONF_MODE="$(stat -c '%a' "$CONF_PATH" 2>/dev/null || echo 'unknown')"
if [ "$CONF_MODE" = "644" ]; then
    pass "Config file '$CONF_PATH' has mode 644"
else
    fail "Config file '$CONF_PATH' has mode 644" "644" "$CONF_MODE"
fi

# Re-running vhost create for the same domain must not exit with an error
# (the tool should detect the existing host and emit a warning/skip, not crash)
DUP_OUTPUT="$(vhost create "$DOMAIN" "$DOC_ROOT" --provider "$PROVIDER" 2>&1 || true)"
if echo "$DUP_OUTPUT" | grep -qiE "already exists|already configured|duplicate"; then
    pass "Duplicate vhost create is rejected gracefully with a clear message"
elif vhost create "$DOMAIN" "$DOC_ROOT" --provider "$PROVIDER" 2>&1; then
    pass "Duplicate vhost create is idempotent (exits 0)"
else
    fail "Duplicate vhost create is handled gracefully (no crash)" "exit 0 or clear error" "$DUP_OUTPUT"
fi

# --------------------------------------------------------------------------
# STEP 5d: vhost list and vhost info smoke tests
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 5d: vhost list and vhost info smoke tests ==="

LIST_OUTPUT="$(vhost list 2>&1)"
assert_output_contains \
    "vhost list shows '$DOMAIN' after create" \
    "$DOMAIN" \
    "$LIST_OUTPUT"

INFO_OUTPUT="$(vhost info "$DOMAIN" 2>&1)"
assert_output_contains \
    "vhost info shows domain name" \
    "$DOMAIN" \
    "$INFO_OUTPUT"

assert_output_contains \
    "vhost info shows provider" \
    "$PROVIDER" \
    "$INFO_OUTPUT"

assert_output_contains \
    "vhost info shows document root" \
    "$DOC_ROOT" \
    "$INFO_OUTPUT"


# --------------------------------------------------------------------------
# STEP 5e: Runtime mode & template integration tests
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 5e: Runtime mode and template integration tests ==="

RUNTIME_DOMAIN="runtimetest.local"
RUNTIME_ROOT="/tmp/runtimetest"
mkdir -p "$RUNTIME_ROOT"

# Helper: get the rendered config path for the current provider/distro
get_conf_path() {
    local domain="$1"
    if [ "$PROVIDER" = "nginx" ]; then
        if [ "$DISTRO" = "ubuntu" ]; then
            echo "/etc/nginx/sites-available/${domain}.conf"
        else
            echo "/etc/nginx/conf.d/${domain}.conf"
        fi
    else
        if [ "$DISTRO" = "ubuntu" ]; then
            echo "/etc/apache2/sites-available/${domain}.conf"
        else
            echo "/etc/httpd/conf.d/${domain}.conf"
        fi
    fi
}

# Helper: create, assert, then remove a runtime-test vhost
runtime_test() {
    local desc="$1"
    local extra_flags="$2"
    local pattern="$3"
    local negate="${4:-false}"   # pass "negate" to assert absence

    local out
    out="$(vhost create "$RUNTIME_DOMAIN" "$RUNTIME_ROOT" \
        --provider "$PROVIDER" --skip-permissions --no-scaffold \
        $extra_flags 2>&1)"
    local exit_code=$?

    if [ "$exit_code" -ne 0 ]; then
        fail "$desc — vhost create failed" "exit 0" "exit $exit_code: $out"
        return
    fi

    local conf_path
    conf_path="$(get_conf_path "$RUNTIME_DOMAIN")"

    if [ "$negate" = "negate" ]; then
        if grep -qE "$pattern" "$conf_path" 2>/dev/null; then
            fail "$desc — pattern must be ABSENT in config" "absent: $pattern" "found in $conf_path"
        else
            pass "$desc"
        fi
    else
        if grep -qE "$pattern" "$conf_path" 2>/dev/null; then
            pass "$desc"
        else
            fail "$desc — pattern not found in config" "$pattern" "$(head -5 "$conf_path" 2>/dev/null)"
        fi
    fi

    # Tear down between sub-tests so the next create is clean
    vhost remove "$RUNTIME_DOMAIN" --provider "$PROVIDER" --force >/dev/null 2>&1 || true
}

# --- Static runtime (default) ---
runtime_test \
    "Static runtime: config contains document root directive" \
    "--runtime static" \
    "(root|DocumentRoot)" 

runtime_test \
    "Static runtime: config does NOT contain proxy_pass/ProxyPass" \
    "--runtime static" \
    "(proxy_pass|ProxyPass)" \
    "negate"

# --- Node.js runtime ---
runtime_test \
    "Node.js runtime (--nodejs): config contains reverse proxy directive" \
    "--nodejs --node-port 4000" \
    "(proxy_pass|ProxyPass)"

runtime_test \
    "Node.js runtime (--nodejs): config references upstream port 4000" \
    "--nodejs --node-port 4000" \
    "4000"

runtime_test \
    "Node.js runtime (--runtime nodejs): config contains reverse proxy directive" \
    "--runtime nodejs --node-port 5000" \
    "(proxy_pass|ProxyPass)"

# --- Python runtime ---
runtime_test \
    "Python runtime (--python): config contains reverse proxy directive to python port" \
    "--python --python-port 9000" \
    "(proxy_pass|ProxyPass)"

runtime_test \
    "Python runtime (--python): config references upstream port 9000" \
    "--python --python-port 9000" \
    "9000"

# --- Custom --template flag ---
# The "static" named template exists for both providers; verify it renders without error
STATIC_TPL_OUT="$(vhost create "$RUNTIME_DOMAIN" "$RUNTIME_ROOT" \
    --provider "$PROVIDER" --skip-permissions --no-scaffold \
    --template static 2>&1)"
STATIC_TPL_EXIT=$?
if [ "$STATIC_TPL_EXIT" -eq 0 ]; then
    pass "--template static: vhost create exits 0"
    STATIC_CONF="$(get_conf_path "$RUNTIME_DOMAIN")"
    if grep -qiE "(root|DocumentRoot)" "$STATIC_CONF" 2>/dev/null; then
        pass "--template static: rendered config contains document root directive"
    else
        fail "--template static: rendered config contains document root directive" \
            "(root|DocumentRoot)" "not found in $STATIC_CONF"
    fi
    vhost remove "$RUNTIME_DOMAIN" --provider "$PROVIDER" --force >/dev/null 2>&1 || true
else
    fail "--template static: vhost create exits 0" "exit 0" "exit $STATIC_TPL_EXIT: $STATIC_TPL_OUT"
fi

# --- --skip-permissions flag ---
SKIP_PERMS_OUT="$(vhost create "$RUNTIME_DOMAIN" "$RUNTIME_ROOT" \
    --provider "$PROVIDER" --skip-permissions --no-scaffold 2>&1)"
SKIP_PERMS_EXIT=$?
if [ "$SKIP_PERMS_EXIT" -eq 0 ]; then
    pass "--skip-permissions: vhost create exits 0 without running chown/chmod"
else
    fail "--skip-permissions: vhost create exits 0 without running chown/chmod" \
        "exit 0" "exit $SKIP_PERMS_EXIT: $SKIP_PERMS_OUT"
fi
vhost remove "$RUNTIME_DOMAIN" --provider "$PROVIDER" --force >/dev/null 2>&1 || true

# --- --no-create-dir guard ---
NODIR_DOMAIN="nodir.local"
NODIR_ROOT="/tmp/this_path_does_not_exist_$$"
NODIR_OUT="$(vhost create "$NODIR_DOMAIN" "$NODIR_ROOT" \
    --provider "$PROVIDER" --no-create-dir 2>&1 || true)"
NODIR_CONF="$(get_conf_path "$NODIR_DOMAIN")"
if [ ! -e "$NODIR_CONF" ]; then
    pass "--no-create-dir: aborts without creating config when doc root is absent"
else
    fail "--no-create-dir: aborts without creating config when doc root is absent" \
        "no config created" "config found at $NODIR_CONF"
    vhost remove "$NODIR_DOMAIN" --provider "$PROVIDER" --force >/dev/null 2>&1 || true
fi

# Cleanup runtime test root
rm -rf "$RUNTIME_ROOT" 2>/dev/null || true


echo ""
echo "=== STEP 6: Disabling virtual host ($DOMAIN) ==="

DISABLE_OUTPUT="$(vhost disable "$DOMAIN" --provider "$PROVIDER" 2>&1)"
DISABLE_EXIT=$?

if [ "$DISABLE_EXIT" -eq 0 ]; then
    pass "vhost disable '$DOMAIN' exits with code 0"
else
    fail "vhost disable '$DOMAIN' exits with code 0" "0" "$DISABLE_EXIT"
fi

assert_hosts_not_contains \
    "/etc/hosts does not contain '$DOMAIN' after disable" \
    "$DOMAIN"

if [ "$PROVIDER" = "nginx" ]; then
    if [ "$DISTRO" = "ubuntu" ]; then
        assert_symlink_not_exists \
            "Nginx symlink removed from /etc/nginx/sites-enabled/$DOMAIN.conf" \
            "/etc/nginx/sites-enabled/$DOMAIN.conf"
        assert_file_exists \
            "Nginx config preserved at /etc/nginx/sites-available/$DOMAIN.conf" \
            "/etc/nginx/sites-available/$DOMAIN.conf"
    elif [ "$DISTRO" = "fedora" ]; then
        assert_file_not_exists \
            "Nginx active config removed from /etc/nginx/conf.d/$DOMAIN.conf" \
            "/etc/nginx/conf.d/$DOMAIN.conf"
        assert_file_exists \
            "Nginx config moved to /etc/nginx/conf.disabled/$DOMAIN.conf" \
            "/etc/nginx/conf.disabled/$DOMAIN.conf"
    fi
elif [ "$PROVIDER" = "apache" ]; then
    if [ "$DISTRO" = "ubuntu" ]; then
        assert_symlink_not_exists \
            "Apache symlink removed from /etc/apache2/sites-enabled/$DOMAIN.conf" \
            "/etc/apache2/sites-enabled/$DOMAIN.conf"
        assert_file_exists \
            "Apache config preserved at /etc/apache2/sites-available/$DOMAIN.conf" \
            "/etc/apache2/sites-available/$DOMAIN.conf"
    elif [ "$DISTRO" = "fedora" ]; then
        assert_file_not_exists \
            "Apache active config removed from /etc/httpd/conf.d/$DOMAIN.conf" \
            "/etc/httpd/conf.d/$DOMAIN.conf"
        assert_file_exists \
            "Apache config moved to /etc/httpd/conf.disabled/$DOMAIN.conf" \
            "/etc/httpd/conf.disabled/$DOMAIN.conf"
    fi
fi

# --------------------------------------------------------------------------
# STEP 7: Enable virtual host
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 7: Enabling virtual host ($DOMAIN) ==="

ENABLE_OUTPUT="$(vhost enable "$DOMAIN" --provider "$PROVIDER" 2>&1)"
ENABLE_EXIT=$?

if [ "$ENABLE_EXIT" -eq 0 ]; then
    pass "vhost enable '$DOMAIN' exits with code 0"
else
    fail "vhost enable '$DOMAIN' exits with code 0" "0" "$ENABLE_EXIT"
fi

assert_hosts_contains \
    "/etc/hosts contains '$DOMAIN' after enable" \
    "$DOMAIN"

if [ "$PROVIDER" = "nginx" ]; then
    if [ "$DISTRO" = "ubuntu" ]; then
        assert_symlink_exists \
            "Nginx symlink restored at /etc/nginx/sites-enabled/$DOMAIN.conf" \
            "/etc/nginx/sites-enabled/$DOMAIN.conf"
    elif [ "$DISTRO" = "fedora" ]; then
        assert_file_exists \
            "Nginx active config restored at /etc/nginx/conf.d/$DOMAIN.conf" \
            "/etc/nginx/conf.d/$DOMAIN.conf"
    fi
elif [ "$PROVIDER" = "apache" ]; then
    if [ "$DISTRO" = "ubuntu" ]; then
        assert_symlink_exists \
            "Apache symlink restored at /etc/apache2/sites-enabled/$DOMAIN.conf" \
            "/etc/apache2/sites-enabled/$DOMAIN.conf"
    elif [ "$DISTRO" = "fedora" ]; then
        assert_file_exists \
            "Apache active config restored at /etc/httpd/conf.d/$DOMAIN.conf" \
            "/etc/httpd/conf.d/$DOMAIN.conf"
    fi
fi

# --------------------------------------------------------------------------
# STEP 8: Remove virtual host
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 8: Removing virtual host ($DOMAIN) ==="

REMOVE_OUTPUT="$(vhost remove "$DOMAIN" --provider "$PROVIDER" --force 2>&1)"
REMOVE_EXIT=$?

if [ "$REMOVE_EXIT" -eq 0 ]; then
    pass "vhost remove '$DOMAIN' --force exits with code 0"
else
    fail "vhost remove '$DOMAIN' --force exits with code 0" "0" "$REMOVE_EXIT"
fi

assert_hosts_not_contains \
    "/etc/hosts does not contain '$DOMAIN' after remove" \
    "$DOMAIN"

if [ "$PROVIDER" = "nginx" ]; then
    if [ "$DISTRO" = "ubuntu" ]; then
        assert_file_not_exists \
            "Nginx config deleted from /etc/nginx/sites-available/$DOMAIN.conf" \
            "/etc/nginx/sites-available/$DOMAIN.conf"
        assert_symlink_not_exists \
            "Nginx symlink deleted from /etc/nginx/sites-enabled/$DOMAIN.conf" \
            "/etc/nginx/sites-enabled/$DOMAIN.conf"
    elif [ "$DISTRO" = "fedora" ]; then
        assert_file_not_exists \
            "Nginx config deleted from /etc/nginx/conf.d/$DOMAIN.conf" \
            "/etc/nginx/conf.d/$DOMAIN.conf"
    fi
elif [ "$PROVIDER" = "apache" ]; then
    if [ "$DISTRO" = "ubuntu" ]; then
        assert_file_not_exists \
            "Apache config deleted from /etc/apache2/sites-available/$DOMAIN.conf" \
            "/etc/apache2/sites-available/$DOMAIN.conf"
        assert_symlink_not_exists \
            "Apache symlink deleted from /etc/apache2/sites-enabled/$DOMAIN.conf" \
            "/etc/apache2/sites-enabled/$DOMAIN.conf"
    elif [ "$DISTRO" = "fedora" ]; then
        assert_file_not_exists \
            "Apache config deleted from /etc/httpd/conf.d/$DOMAIN.conf" \
            "/etc/httpd/conf.d/$DOMAIN.conf"
    fi
fi

# --------------------------------------------------------------------------
# STEP 9: Post-removal validation
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 9: Post-removal validation ==="

LIST_OUTPUT="$(vhost list 2>&1)"
assert_output_not_contains \
    "vhost list does not show '$DOMAIN' after removal" \
    "$DOMAIN" \
    "$LIST_OUTPUT"

# vhost info exits 0 but prints a "not found" message — that satisfies the PRD.
INFO_OUTPUT="$(vhost info "$DOMAIN" 2>&1 || true)"
assert_output_contains \
    "vhost info indicates domain is not found" \
    "No configuration found" \
    "$INFO_OUTPUT"

# --------------------------------------------------------------------------
# STEP 10: Uninstall VHost Helper
# --------------------------------------------------------------------------
echo ""
echo "=== STEP 10: Running uninstall.sh --deep-clean ==="

if bash "$SRC_DIR/uninstall.sh" --deep-clean; then
    pass "uninstall.sh --deep-clean completed with exit code 0"
else
    fail "uninstall.sh --deep-clean completed with exit code 0" "exit 0" "exit $?"
fi

assert_file_not_exists \
    "vhost binary removed from /usr/local/bin/vhost" \
    "/usr/local/bin/vhost"

assert_file_not_exists \
    "Installation directory removed: /opt/vhost-helper" \
    "/opt/vhost-helper"

assert_file_not_exists \
    "Bash completion removed from /etc/bash_completion.d/vhost" \
    "/etc/bash_completion.d/vhost"

# Verify the vhost command is no longer accessible in PATH.
# Flush bash's command-lookup cache first so a stale hash entry
# cannot cause a false positive after the binary has been removed.
hash -r 2>/dev/null || true
if ! command -v vhost > /dev/null 2>&1; then
    pass "vhost command is no longer accessible in PATH"
else
    fail "vhost command is no longer accessible in PATH" "not found" "$(command -v vhost)"
fi

# --------------------------------------------------------------------------
# Final summary
# --------------------------------------------------------------------------
print_summary

[ "$FAIL_COUNT" -eq 0 ]
