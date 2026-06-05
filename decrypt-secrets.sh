#!/usr/bin/env bash
# decrypt-secrets.sh — Decrypt repo secrets for container/runtime startup (Ubuntu/Linux)
#
# Decrypts baked-in ciphertext next to this script and writes plaintext beside them:
#   .env.age              -> .env
#   arweave-keyfile-....json.age -> arweave-keyfile-....json
#
# Private keys must be supplied at runtime (never bake keys into the image):
#   AGE_SECRET_KEY_ENV      — full age identity for .env.age (multiline ok)
#   AGE_SECRET_KEY_ARWEAVE  — full age identity for the arweave keyfile .age
#
# Alternatives (either secret var above OR its _B64 / _FILE companion):
#   AGE_SECRET_KEY_ENV_B64 / AGE_SECRET_KEY_ARWEAVE_B64  — base64-encoded identity
#   AGE_SECRET_KEY_ENV_FILE / AGE_SECRET_KEY_ARWEAVE_FILE — path to identity file
#
# Optional:
#   REVIEW_GENERATOR_ROOT — directory containing the .age files (default: script dir)
#   AGE_BIN               — path to age binary
#
# Example (docker run):
#   docker run --rm \
#     -e AGE_SECRET_KEY_ENV="$(cat .env.age-key.txt)" \
#     -e AGE_SECRET_KEY_ARWEAVE="$(cat arweave-keyfile-....json.age-key.txt)" \
#     -v "$PWD:/app" -w /app myimage ./decrypt-secrets.sh

set -euo pipefail

readonly ARWEAVE_AGE_NAME='arweave-keyfile-jOMEW4KCKkghYzpW6rB8SiUXJa1TvnWZf-Kxck94yn8.json.age'
readonly ENV_AGE_NAME='.env.age'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${REVIEW_GENERATOR_ROOT:-$SCRIPT_DIR}"

resolve_bin() {
  if [[ -n "${AGE_BIN:-}" ]]; then
    if [[ -x "$AGE_BIN" ]]; then
      echo "$AGE_BIN"
      return
    fi
    echo "error: AGE_BIN='$AGE_BIN' is not executable" >&2
    exit 1
  fi
  if command -v age >/dev/null 2>&1; then
    command -v age
    return
  fi
  echo 'error: age not found on PATH. Install: apt install age' >&2
  exit 1
}

# Resolve key material from AGE_SECRET_KEY_<NAME>, or <NAME>_B64, or <NAME>_FILE.
load_identity() {
  local base_var="$1"
  local file_var="${base_var}_FILE"
  local b64_var="${base_var}_B64"
  local material path

  if [[ -n "${!file_var:-}" ]]; then
    path="${!file_var}"
    if [[ ! -f "$path" ]]; then
      echo "error: ${file_var}='$path' not found" >&2
      return 1
    fi
    cat "$path"
    return 0
  fi

  if [[ -n "${!b64_var:-}" ]]; then
    printf '%s' "${!b64_var}" | base64 -d
    return 0
  fi

  material="${!base_var:-}"
  if [[ -z "$material" ]]; then
    echo "error: set ${base_var}, ${b64_var}, or ${file_var}" >&2
    return 1
  fi

  # Allow pointing at a mounted secret path via the base var.
  if [[ -f "$material" ]]; then
    cat "$material"
    return 0
  fi

  printf '%s' "$material"
}

decrypt_one() {
  local encrypted_name="$1"
  local key_var="$2"
  local encrypted="${ROOT}/${encrypted_name}"
  local plain="${encrypted%.age}"
  local age key_body key_tmp

  if [[ ! -f "$encrypted" ]]; then
    echo "error: encrypted file not found: $encrypted" >&2
    return 1
  fi

  age="$(resolve_bin)"
  key_body="$(load_identity "$key_var")" || return 1

  echo "Decrypting $(basename "$encrypted") -> $(basename "$plain")"
  (
    key_tmp="$(mktemp)"
    chmod 600 "$key_tmp"
    printf '%s\n' "$key_body" >"$key_tmp"
    "$age" -d -i "$key_tmp" -o "$plain" "$encrypted"
  )
}

main() {
  local failed=0

  if ! decrypt_one "$ENV_AGE_NAME" 'AGE_SECRET_KEY_ENV'; then
    failed=1
  fi
  if ! decrypt_one "$ARWEAVE_AGE_NAME" 'AGE_SECRET_KEY_ARWEAVE'; then
    failed=1
  fi

  if [[ "$failed" -ne 0 ]]; then
    exit 1
  fi

  echo
  echo 'Secrets ready:'
  echo "  ${ROOT}/.env"
  echo "  ${ROOT}/${ARWEAVE_AGE_NAME%.age}"
}

main "$@"
