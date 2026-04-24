#!/bin/bash
set -e

# VHost Helper Installer
# This script installs VHost Helper globally on Linux.

# 1. Check permissions
if [ "$EUID" -ne 0 ]; then
  echo "Error: Please run as root (sudo bash install.sh)"
  exit 1
fi

echo "Installing VHost Helper..."

# 2. Identify source directory (where the script is located)
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VHOST_BIN="$SOURCE_DIR/bin/vhost"

if [ ! -f "$VHOST_BIN" ]; then
    echo "Error: Could not find vhost binary at $VHOST_BIN"
    exit 1
fi

# 3. Check Python version (3.10+)
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is not installed."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
REQUIRED_VERSION="3.10"

if [[ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]]; then
    echo "Error: Python $REQUIRED_VERSION+ is required. Found $PYTHON_VERSION"
    exit 1
fi

# 4. Deploy to /opt/vhost-helper and create global symlink
INSTALL_PATH="/opt/vhost-helper"
echo "Deploying to $INSTALL_PATH..."

# Create installation directory
mkdir -p "$INSTALL_PATH"

# Copy necessary files and directories
for item in bin lib templates requirements.txt README.md; do
    if [ -e "$SOURCE_DIR/$item" ]; then
        cp -r "$SOURCE_DIR/$item" "$INSTALL_PATH/"
    fi
done

# 5. Create virtual environment and install dependencies
echo "Setting up Python virtual environment..."
if ! python3 -m venv "$INSTALL_PATH/.venv"; then
    echo "Error: Failed to create virtual environment. Do you have python3-venv installed?"
    exit 1
fi
"$INSTALL_PATH/.venv/bin/pip" install -r "$INSTALL_PATH/requirements.txt"

# Update the shebang of the vhost binary to use the venv's Python
# This ensures it runs with the isolated dependencies
sed -i "1s|.*|#!$INSTALL_PATH/.venv/bin/python3|" "$INSTALL_PATH/bin/vhost"

# Ensure root ownership for security
chown -R root:root "$INSTALL_PATH"
chmod -R 755 "$INSTALL_PATH"
chmod +x "$INSTALL_PATH/bin/vhost"
chmod +x "$INSTALL_PATH/bin/detect_os.sh"

echo "Creating global symlink /usr/local/bin/vhost..."
ln -sf "$INSTALL_PATH/bin/vhost" /usr/local/bin/vhost


# 6. Configure Bash Autocompletion
echo "Configuring Bash autocompletion..."
# Create the directory if it doesn't exist (e.g. on newer Ubuntu where bash-completion might not create it by default)
mkdir -p /etc/bash_completion.d

cat << 'EOF' > /etc/bash_completion.d/vhost
_vhost_completion() {
    local IFS=$'\n'
    COMPREPLY=( $( env COMP_WORDS="${COMP_WORDS[*]}" \
                   COMP_CWORD=$COMP_CWORD \
                   _VHOST_COMPLETE=complete_bash $1 ) )
    return 0
}
complete -o default -F _vhost_completion vhost
EOF

if [ -s /etc/bash_completion.d/vhost ]; then
    echo "Autocompletion installed to /etc/bash_completion.d/vhost"
else
    echo "Warning: Could not generate autocompletion script automatically."
fi


echo "--------------------------------------------------"
echo "VHost Helper installed successfully!"
echo "You can now run 'vhost --help' from any directory."
echo "Note: You may need to restart your shell for autocompletion to take effect."
