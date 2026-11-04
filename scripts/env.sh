# Source from scripts: load .env without overriding variables already set (same rule as bankql.config.load_dotenv).
if [ -f .env ]; then
  while IFS='=' read -r _k _v; do
    [[ "$_k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [ -z "${!_k+x}" ]; then export "$_k=$_v"; fi
  done < .env
fi
true  # a sourced file must not end with a failing status under `set -e`
