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
# STEP 6: Disable virtual host
# --------------------------------------------------------------------------
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

# Verify the vhost command is no longer accessible in PATH
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
