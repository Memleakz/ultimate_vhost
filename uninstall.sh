#!/bin/bash
set -e

# VHost Helper Uninstaller
# This script removes VHost Helper from the system.

# 1. Check permissions
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (sudo bash uninstall.sh)"
  exit 1
fi

DEEP_CLEAN=false
if [[ "$1" == "--deep-clean" ]]; then
    DEEP_CLEAN=true
fi

echo "Uninstalling VHost Helper..."

# 1. Remove global symlink
if [ -L /usr/local/bin/vhost ] || [ -f /usr/local/bin/vhost ]; then
    rm -f /usr/local/bin/vhost
    echo "Removed /usr/local/bin/vhost"
fi

# 2. Remove installation directory
if [ -d /opt/vhost-helper ]; then
    rm -rf /opt/vhost-helper
    echo "Removed /opt/vhost-helper"
fi

# 3. Remove autocompletion
if [ -f /etc/bash_completion.d/vhost ]; then
    rm -f /etc/bash_completion.d/vhost
    echo "Removed /etc/bash_completion.d/vhost"
fi

# 3. Deep clean
if [ "$DEEP_CLEAN" = true ]; then
    echo "Performing deep clean..."
    # The requirement says "remove system logs and application-specific configuration files"
    # but "leave /etc/nginx/sites-available/ and /var/www/ directories untouched".
    
    # Remove system logs if any
    if [ -f /var/log/vhost.log ]; then
        rm -f /var/log/vhost.log
        echo "Removed /var/log/vhost.log"
    fi
    
    # Remove application-specific configuration files
    # (Currently none defined outside of Nginx/Apache which must be preserved)
    # If we had a ~/.vhost_helper or /etc/vhost_helper.conf, we would remove it here.
    
    echo "Deep clean complete (user data in /etc/nginx/ and /var/www/ preserved)."
fi

echo "--------------------------------------------------"
echo "VHost Helper uninstalled successfully."
