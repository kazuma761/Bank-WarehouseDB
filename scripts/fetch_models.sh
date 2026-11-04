#!/usr/bin/env bash
# Download the two GGUF files named in config/models.yaml into ./models (needs huggingface.co reachable; HF_TOKEN if gated).
set -euo pipefail
cd "$(dirname "$0")/.."
for role in router sql; do
  read -r repo file < <(.venv/bin/python -c "import yaml;m=yaml.safe_load(open('config/models.yaml'))['$role'];print(m['hf_repo'], m['hf_file'])")
  [ -f "models/$file" ] && { echo "have models/$file"; continue; }
  uvx --from "huggingface_hub[cli]" hf download "$repo" "$file" --local-dir models
done
