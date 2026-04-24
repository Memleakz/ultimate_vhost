#!/bin/bash
# detect_os.sh - Identify distribution-specific configurations
# Part of VHost Helper Project

set -e

# Initialize variables
OS_ID=""
OS_VERSION=""

# Primary source of truth: /etc/os-release
if [[ -f /etc/os-release ]]; then
    # Source the file to get ID and VERSION_ID
    # We use a subshell to avoid polluting current environment
    # but since we're in a script it's already isolated.
    # We use 'grep' to extract values directly to be safer than 'source'
    OS_ID=$(grep -E '^ID=' /etc/os-release | cut -d'=' -f2 | tr -d '"' | tr -d "'" | tr '[:upper:]' '[:lower:]')
    OS_VERSION=$(grep -E '^VERSION_ID=' /etc/os-release | cut -d'=' -f2 | tr -d '"' | tr -d "'")
    
    # Fallback to VERSION if VERSION_ID is missing
    if [[ -z "$OS_VERSION" ]]; then
        OS_VERSION=$(grep -E '^VERSION=' /etc/os-release | cut -d'=' -f2 | tr -d '"' | tr -d "'")
    fi
fi

# Fallback mechanisms for legacy systems
if [[ -z "$OS_ID" ]]; then
    if [[ -f /etc/redhat-release ]]; then
        OS_ID="rhel"
        OS_VERSION=$(sed -rn 's/.*([0-9]+\.[0-9]+).*/\1/p' /etc/redhat-release)
    elif [[ -f /etc/debian_version ]]; then
        OS_ID="debian"
        OS_VERSION=$(cat /etc/debian_version)
    fi
fi

# Supported distributions validation
# Ubuntu, Debian, CentOS, RHEL, Fedora
SUPPORTED_DISTS=("ubuntu" "debian" "centos" "rhel" "fedora")
IS_SUPPORTED=false

if [[ -n "$OS_ID" ]]; then
    for dist in "${SUPPORTED_DISTS[@]}"; do
        if [[ "$OS_ID" == "$dist" ]]; then
            IS_SUPPORTED=true
            break
        fi
    done
fi

if [ "$IS_SUPPORTED" = false ]; then
    echo "Error: Operating system '$OS_ID' is not a supported Linux distribution." >&2
    exit 1
fi

# Output normalized ID and VERSION
echo "ID=$OS_ID"
echo "VERSION=$OS_VERSION"
exit 0
