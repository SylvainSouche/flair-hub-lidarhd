#!/bin/sh
# Upload the packaged dataset folder to the Hugging Face Hub (skips unchanged files).
# Usage: ./upload_hf.sh [/path/to/FLAIR-HUB-LIDARHD] [repo_id]
set -e
HF_DIR=${1:-/Volumes/SAMSUNG/sylvain/huggingface/FLAIR-HUB-LIDARHD}
REPO=${2:-SylvainSouche/FLAIR-HUB-LiDAR-HD}
dot_clean -m "$HF_DIR" 2>/dev/null || true      # macOS AppleDouble ._* files on exFAT drives
hf upload "$REPO" "$HF_DIR" . --repo-type dataset \
  --exclude "._*" --exclude "**/._*" --exclude "*.part" --exclude "**/*.part" \
  --commit-message "Upload packaged data"
