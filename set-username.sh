#!/usr/bin/env bash
# Replace the YOUR-GITHUB-USERNAME placeholder throughout the docs.
#
#   ./set-username.sh your-github-username
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: $0 <github-username>" >&2
  exit 1
fi

USER_NAME="$1"

if [[ ! "$USER_NAME" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?$ ]]; then
  echo "error: '$USER_NAME' doesn't look like a GitHub username" >&2
  echo "       (letters, digits and hyphens; no leading/trailing hyphen)" >&2
  exit 1
fi

cd "$(dirname "$0")"

FILES=(README.md index.html SETUP.md PUBLISHING.md)
found=0

for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  if grep -q 'YOUR-GITHUB-USERNAME' "$f"; then
    # -i '' on BSD/macOS sed, -i on GNU sed
    if sed --version >/dev/null 2>&1; then
      sed -i "s/YOUR-GITHUB-USERNAME/$USER_NAME/g" "$f"
    else
      sed -i '' "s/YOUR-GITHUB-USERNAME/$USER_NAME/g" "$f"
    fi
    echo "  updated $f"
    found=1
  fi
done

if [ "$found" -eq 0 ]; then
  echo "No placeholders left — already set?"
else
  echo
  echo "Your project page will be at:"
  echo "  https://$USER_NAME.github.io/personal-health-record-exporter/"
fi
