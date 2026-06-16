#!/bin/sh
set -e

# Get the absolute path to the script's directory
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
DEPS_DIR="/../deps"

# Check if the deps directory exists
if [ ! -d "$DEPS_DIR" ]; then
    echo "Error: Directory '$DEPS_DIR' not found."
    exit 1
fi

echo "Updating all git repos in '$DEPS_DIR'..."

# Loop through all subdirectories in the deps folder
for dir in "$DEPS_DIR"/*/; do
    # Check if it is a directory and contains a .git folder
    echo "Processing directory: ${dir}"
    if [ -e "${dir}/.git" ]; then
        echo "Updating repo"
        # Use a subshell to change directory, so we don't have to cd back
        (cd "${dir}" && git pull)
    else
        echo "Skipping (not a git repo)"
    fi
    echo "------------------------------------"
done

echo "All repos updated."
