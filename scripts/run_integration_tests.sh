#!/bin/bash
# Integration test runner for VHost Helper.
#
# Orchestrates ephemeral containers (Docker or Podman) for ubuntu:latest and fedora:latest,
# executes a full install → create → disable → enable → remove → uninstall lifecycle on each,
# and prints a structured PASS/FAIL report covering all four distribution+provider combinations.
#
# Matrix:
#   1. Ubuntu  + Nginx
#   2. Ubuntu  + Apache
#   3. Fedora  + Nginx
#   4. Fedora  + Apache
#
# Prerequisites: Docker or Podman installed and running on the host.
#
# Usage:
#   bash src/scripts/run_integration_tests.sh
#
# Exit codes:
#   0 — all four configurations passed
#   1 — one or more configurations failed (or Docker unavailable)

set -uo pipefail

# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# --------------------------------------------------------------------------
# ANSI colour helpers (degrade gracefully if terminal lacks colour support)
# --------------------------------------------------------------------------
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    BOLD='\033[1m'
    NC='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' BOLD='' NC=''
fi

# --------------------------------------------------------------------------
# Container lifecycle management
# --------------------------------------------------------------------------
CONTAINER_PREFIX="vhost-integration"

# Containers to clean up on exit (populated as they are started).
ACTIVE_CONTAINERS=()

cleanup() {
    if [ "${#ACTIVE_CONTAINERS[@]}" -gt 0 ]; then
        echo -e "\n${YELLOW}Pruning containers: ${ACTIVE_CONTAINERS[*]}${NC}"
        "$CONTAINER_ENGINE" rm -f "${ACTIVE_CONTAINERS[@]}" > /dev/null 2>&1 || true
        echo -e "${YELLOW}Cleanup complete.${NC}"
    fi
}

trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------
# Pre-flight checks (Container engine detection)
# --------------------------------------------------------------------------
if command -v docker &> /dev/null; then
    CONTAINER_ENGINE="docker"
elif command -v podman &> /dev/null; then
    CONTAINER_ENGINE="podman"
else
    echo -e "${RED}Error: Neither Docker nor Podman is installed or in PATH.${NC}" >&2
    exit 1
fi

if ! "$CONTAINER_ENGINE" info &> /dev/null 2>&1; then
    echo -e "${RED}Error: $CONTAINER_ENGINE is installed but not running or accessible.${NC}" >&2
    exit 1
fi

if [ ! -f "$SRC_DIR/install.sh" ]; then
    echo -e "${RED}Error: Cannot find src/install.sh at $SRC_DIR/install.sh${NC}" >&2
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/in_container_test.sh" ]; then
    echo -e "${RED}Error: Cannot find in_container_test.sh at $SCRIPT_DIR/in_container_test.sh${NC}" >&2
    exit 1
fi

# --------------------------------------------------------------------------
# Result tracking
# Four parallel arrays keyed by index (bash 3 compatible — no associative arrays).
# --------------------------------------------------------------------------
CONFIG_LABELS=()
CONFIG_IMAGES=()
CONFIG_DISTROS=()
CONFIG_PROVIDERS=()
CONFIG_RESULTS=()
CONFIG_PASS_COUNTS=()
CONFIG_FAIL_COUNTS=()
CONFIG_DETAILS=()

register_config() {
    CONFIG_LABELS+=("$1")
    CONFIG_IMAGES+=("$2")
    CONFIG_DISTROS+=("$3")
    CONFIG_PROVIDERS+=("$4")
    CONFIG_RESULTS+=("pending")
    CONFIG_PASS_COUNTS+=(0)
    CONFIG_FAIL_COUNTS+=(0)
    CONFIG_DETAILS+=("")
}

register_config "Ubuntu  + Nginx"  "ubuntu:latest" "ubuntu" "nginx"
register_config "Ubuntu  + Apache" "ubuntu:latest" "ubuntu" "apache"
register_config "Fedora  + Nginx"  "fedora:latest" "fedora" "nginx"
register_config "Fedora  + Apache" "fedora:latest" "fedora" "apache"

# --------------------------------------------------------------------------
# Per-configuration test runner
# --------------------------------------------------------------------------
run_config_test() {
    local index="$1"
    local label="${CONFIG_LABELS[$index]}"
    local image="${CONFIG_IMAGES[$index]}"
    local distro="${CONFIG_DISTROS[$index]}"
    local provider="${CONFIG_PROVIDERS[$index]}"
    local container="${CONTAINER_PREFIX}-${distro}-${provider}"

    echo -e "\n${BLUE}${BOLD}============================================================${NC}"
    echo -e "${BLUE}${BOLD}  Configuration : ${label}${NC}"
    echo -e "${BLUE}${BOLD}  Image         : ${image}${NC}"
    echo -e "${BLUE}${BOLD}============================================================${NC}"

    # Remove any stale container from a previous interrupted run.
    "$CONTAINER_ENGINE" rm -f "$container" > /dev/null 2>&1 || true

    ACTIVE_CONTAINERS+=("$container")

    # Pull the image quietly so progress bars don't clutter the report.
    echo -e "${YELLOW}Pulling ${image}...${NC}"
    if ! "$CONTAINER_ENGINE" pull --quiet "$image" > /dev/null 2>&1; then
        CONFIG_RESULTS[$index]="FAIL"
        CONFIG_DETAILS[$index]="Could not pull container image: $image"
        echo -e "${RED}  Failed to pull ${image}. Skipping.${NC}"
        return 1
    fi

    # For Podman on systems with SELinux (like Fedora), append :z to the volume
    # mount so the container has permission to read the host files.
    local vol_flags="ro"
    if [ "$CONTAINER_ENGINE" = "podman" ] && command -v getenforce &> /dev/null; then
        if [ "$(getenforce)" != "Disabled" ]; then
            vol_flags="ro,z"
        fi
    fi

    # Capture container output for pass/fail count parsing.
    local tmp_output
    tmp_output="$(mktemp)"

    if "$CONTAINER_ENGINE" run \
        --name "$container" \
        --volume "$SRC_DIR:/opt/vhost-src:${vol_flags}" \
        "$image" \
        bash /opt/vhost-src/scripts/in_container_test.sh "$distro" "$provider" \
        2>&1 | tee "$tmp_output"; then

        CONFIG_RESULTS[$index]="PASS"
        CONFIG_DETAILS[$index]=""
        echo -e "\n${GREEN}${label}: container exited cleanly.${NC}"
    else
        local exit_code=${PIPESTATUS[0]}
        CONFIG_RESULTS[$index]="FAIL"
        CONFIG_DETAILS[$index]="Container exited with code $exit_code — see output above"
        echo -e "\n${RED}${label}: container exited with code ${exit_code}.${NC}"
    fi

    # Parse pass/fail counts from the in-container summary block.
    local pass_count fail_count
    pass_count="$(grep -E '^\s+Passed\s+:' "$tmp_output" | tail -1 | grep -oE '[0-9]+' || echo 0)"
    fail_count="$(grep -E '^\s+Failed\s+:' "$tmp_output" | tail -1 | grep -oE '[0-9]+' || echo 0)"
    CONFIG_PASS_COUNTS[$index]="${pass_count:-0}"
    CONFIG_FAIL_COUNTS[$index]="${fail_count:-0}"

    rm -f "$tmp_output"
}

# --------------------------------------------------------------------------
# Run all configurations sequentially
# --------------------------------------------------------------------------
echo -e "\n${BOLD}VHost Helper — Cross-Distribution Integration Tests (v0.1 Matrix)${NC}"
echo -e "Source directory: ${SRC_DIR}"
echo ""

for i in "${!CONFIG_LABELS[@]}"; do
    run_config_test "$i" || true   # capture failure but continue to next config
done

# --------------------------------------------------------------------------
# Aggregate totals
# --------------------------------------------------------------------------
TOTAL_PASS=0
TOTAL_FAIL=0

for i in "${!CONFIG_LABELS[@]}"; do
    TOTAL_PASS=$(( TOTAL_PASS + CONFIG_PASS_COUNTS[$i] ))
    TOTAL_FAIL=$(( TOTAL_FAIL + CONFIG_FAIL_COUNTS[$i] ))
done

TOTAL_TESTS=$(( TOTAL_PASS + TOTAL_FAIL ))

# --------------------------------------------------------------------------
# Final PASS/FAIL report
# --------------------------------------------------------------------------
echo -e "\n${BOLD}============================================================${NC}"
echo -e "${BOLD}        VHost Helper v0.1 — INTEGRATION TEST REPORT${NC}"
echo -e "${BOLD}============================================================${NC}"
printf "  %-26s  %-8s  %s\n" "Configuration" "Result" "Tests"
echo -e "  ----------------------------------------------------------"

ALL_PASSED=true

for i in "${!CONFIG_LABELS[@]}"; do
    label="${CONFIG_LABELS[$i]}"
    result="${CONFIG_RESULTS[$i]}"
    detail="${CONFIG_DETAILS[$i]}"
    p="${CONFIG_PASS_COUNTS[$i]}"
    f="${CONFIG_FAIL_COUNTS[$i]}"

    if [ "$result" = "PASS" ]; then
        printf "  %-26s  ${GREEN}%-8s${NC}  %d passed / %d failed\n" "$label" "[PASS]" "$p" "$f"
    else
        printf "  %-26s  ${RED}%-8s${NC}  %d passed / %d failed\n" "$label" "[FAIL]" "$p" "$f"
        [ -n "$detail" ] && echo -e "    ${RED}↳ ${detail}${NC}"
        ALL_PASSED=false
    fi
done

echo -e "  ----------------------------------------------------------"
printf "  %-26s  %-8s  %d passed / %d failed\n" "TOTAL" "" "$TOTAL_PASS" "$TOTAL_FAIL"
echo -e "  Total tests executed: ${TOTAL_TESTS}"
echo -e "${BOLD}============================================================${NC}"

if $ALL_PASSED; then
    echo -e "${GREEN}${BOLD}VHost Helper v0.1: READY FOR RELEASE${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}One or more configurations failed. NOT ready for release.${NC}"
    exit 1
fi
