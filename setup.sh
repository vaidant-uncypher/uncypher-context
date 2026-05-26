#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ENV_FILE="${SCRIPT_DIR}/.context.env"

default_vault="${HOME}/Obsidian/Memory"

printf "Obsidian vault path [%s]: " "${default_vault}"
read -r vault_path
vault_path="${vault_path:-${default_vault}}"

mkdir -p "${vault_path}"

cat > "${ENV_FILE}" <<EOF
CONTEXT_VAULT_PATH="${vault_path}"
EOF

chmod +x "${SCRIPT_DIR}/context-safe.sh"

"${SCRIPT_DIR}/context-safe.sh" memory_init
"${SCRIPT_DIR}/context-safe.sh" memory_compile

cat <<EOF

Setup complete.

Vault: ${vault_path}
Config: ${ENV_FILE}

Next:
  ./context-safe.sh start_session --title "First memory session"
  ./context-safe.sh memory_capture "Example source" --kind note --text "A useful idea" --threads "getting-started"
  ./context-safe.sh memory_compile
EOF
