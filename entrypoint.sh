#!/usr/bin/env bash
# entrypoint.sh — Container startup for Review-Generator
#
# Phases: decrypt secrets → load .env → install deps → download models →
#         start three llama-server instances → wait for APIs →
#         status_relay.py (background) + orchestrate.py
#
# Runtime secrets (required for full runs — two different age identities):
#   AGE_SECRET_KEY_ENV      — decrypts baked .env.age → .env
#   AGE_SECRET_KEY_ARWEAVE  — decrypts baked arweave-keyfile-….json.age
#   Or use *_B64 / *_FILE variants (see decrypt-secrets.sh).
#
# Other runtime:
#   docker run --gpus all  (all llama-server processes use --n-gpu-layers 99)
#   Mount a volume on MODELS_ROOT (and/or HF_HOME) to persist GGUF downloads.
#
# All python/node deps install globally inside the container (no venv).
# Whisper model path/bin are read from decrypted .env (WHISPER_*).
#
# Optional overrides:
#   REVIEW_GENERATOR_ROOT, MODELS_ROOT, LLAMA_SERVER_BIN, HF_HOME, PYTHON,
#   PIP_INSTALL_MARKER, LLAMA_WAIT_ATTEMPTS, LLAMA_WAIT_INTERVAL_SEC
#
# Example:
#   docker run --rm --gpus all \
#     -e AGE_SECRET_KEY_ENV="$(cat .env.age-key.txt)" \
#     -e AGE_SECRET_KEY_ARWEAVE="$(cat arweave-keyfile-jOMEW4KCKkghYzpW6rB8SiUXJa1TvnWZf-Kxck94yn8.json.age-key.txt)" \
#     -v review-models:/app/models \
#     -w /app your-image
#
# CMD passthrough: docker run … your-image --test --test-limit 3

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REVIEW_GENERATOR_ROOT="${REVIEW_GENERATOR_ROOT:-$SCRIPT_DIR}"
MODELS_ROOT="${MODELS_ROOT:-$REVIEW_GENERATOR_ROOT/models}"
LLAMA_SERVER_BIN="${LLAMA_SERVER_BIN:-/opt/llama.cpp/build/bin/llama-server}"
HF_HOME="${HF_HOME:-$MODELS_ROOT/hf-cache}"
PYTHON="${PYTHON:-python3}"
PIP_INSTALL_MARKER="${PIP_INSTALL_MARKER:-$REVIEW_GENERATOR_ROOT/.entrypoint-pip-done}"
NPM_INSTALL_MARKER="${NPM_INSTALL_MARKER:-$REVIEW_GENERATOR_ROOT/.entrypoint-npm-done}"
CRAWL4AI_INSTALL_MARKER="${CRAWL4AI_INSTALL_MARKER:-$REVIEW_GENERATOR_ROOT/.entrypoint-crawl4ai-done}"
LOG_DIR="${LOG_DIR:-$REVIEW_GENERATOR_ROOT/logs}"

LLAMA_WAIT_ATTEMPTS="${LLAMA_WAIT_ATTEMPTS:-60}"
LLAMA_WAIT_INTERVAL_SEC="${LLAMA_WAIT_INTERVAL_SEC:-10}"

export REVIEW_GENERATOR_ROOT MODELS_ROOT HF_HOME

LLAMA_PIDS=()

log() {
  printf '[entrypoint] %s\n' "$*"
}

die() {
  printf '[entrypoint] error: %s\n' "$*" >&2
  exit 1
}

# True if VAR, VAR_B64, or VAR_FILE is set (matches decrypt-secrets.sh).
age_key_configured() {
  local base="$1"
  local file_var="${base}_FILE"
  local b64_var="${base}_B64"
  [[ -n "${!base:-}" || -n "${!b64_var:-}" || -n "${!file_var:-}" ]]
}

preflight_age_keys() {
  if ! age_key_configured 'AGE_SECRET_KEY_ENV'; then
    die 'set AGE_SECRET_KEY_ENV, AGE_SECRET_KEY_ENV_B64, or AGE_SECRET_KEY_ENV_FILE'
  fi
  if ! age_key_configured 'AGE_SECRET_KEY_ARWEAVE'; then
    die 'set AGE_SECRET_KEY_ARWEAVE, AGE_SECRET_KEY_ARWEAVE_B64, or AGE_SECRET_KEY_ARWEAVE_FILE'
  fi
}

load_dotenv() {
  local env_path="$REVIEW_GENERATOR_ROOT/.env"
  [[ -f "$env_path" ]] || die "missing $env_path (run decrypt-secrets.sh first)"

  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    [[ "$line" != *"="* ]] && continue
    key="${line%%=*}"
    key="${key%"${key##*[![:space:]]}"}"
    key="${key#"${key%%[![:space:]]*}"}"
    [[ -z "$key" ]] && continue
    if [[ -n "${!key+x}" ]]; then
      continue
    fi
    val="${line#*=}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    if [[ "$val" == \"*\" && "$val" == *\" ]]; then
      val="${val:1:${#val}-2}"
    elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
      val="${val:1:${#val}-2}"
    fi
    export "$key=$val"
  done <"$env_path"
}

sync_hf_token_alias() {
  if [[ -n "${HF_TOKEN:-}" && -z "${HUGGING_FACE_HUB_TOKEN:-}" ]]; then
    export HUGGING_FACE_HUB_TOKEN="$HF_TOKEN"
  elif [[ -n "${HUGGING_FACE_HUB_TOKEN:-}" && -z "${HF_TOKEN:-}" ]]; then
    export HF_TOKEN="$HUGGING_FACE_HUB_TOKEN"
  fi
}

require_hf_token() {
  sync_hf_token_alias
  [[ -n "${HF_TOKEN:-}" ]] || die 'HF_TOKEN is empty in .env (needed for hf downloads)'
}

# Extract TCP port from http://host:port/path (default if unset).
port_from_url() {
  local url="$1"
  local default_port="$2"
  local port="${url##*:}"
  port="${port%%/*}"
  if [[ -z "$port" || "$port" == "$url" ]]; then
    echo "$default_port"
  else
    echo "$port"
  fi
}

decrypt_secrets() {
  preflight_age_keys
  log 'decrypting baked secrets'
  (
    cd "$REVIEW_GENERATOR_ROOT"
    ./decrypt-secrets.sh
  )
  [[ -f "$REVIEW_GENERATOR_ROOT/.env" ]] || die 'decrypt did not produce .env'
  # Discover whichever arweave-keyfile-*.json was just decrypted (no hardcoded
  # wallet name — decrypt-secrets.sh is the source of truth for which one).
  local arweave_jsons=("$REVIEW_GENERATOR_ROOT"/arweave-keyfile-*.json)
  if [[ ! -f "${arweave_jsons[0]}" ]]; then
    die 'decrypt did not produce any arweave-keyfile-*.json'
  fi
  if (( ${#arweave_jsons[@]} > 1 )); then
    log "warning: multiple arweave-keyfile-*.json present (${#arweave_jsons[@]}); using $(basename "${arweave_jsons[0]}")"
  fi
  export ARWEAVE_JSON_PATH="${arweave_jsons[0]}"
}

install_python_deps() {
  if [[ -f "$PIP_INSTALL_MARKER" ]]; then
    log "python deps already installed ($PIP_INSTALL_MARKER)"
    return 0
  fi
  log 'installing python dependencies'
  "$PYTHON" -m pip install --no-cache-dir -r "$REVIEW_GENERATOR_ROOT/requirements.txt"
  touch "$PIP_INSTALL_MARKER"
}

install_node_deps() {
  if [[ -f "$NPM_INSTALL_MARKER" ]]; then
    log "node deps already installed ($NPM_INSTALL_MARKER)"
    return 0
  fi
  command -v npm >/dev/null 2>&1 || die 'npm not on PATH (bake Node.js into the image)'

  log 'installing node dependencies (uploader)'
  npm install --prefix "$REVIEW_GENERATOR_ROOT/uploader"

  log 'installing node dependencies (molecule crawler)'
  npm install --prefix "$REVIEW_GENERATOR_ROOT/crawlers/molecule/crawler"

  touch "$NPM_INSTALL_MARKER"
}

install_crawl4ai_browsers() {
  if [[ -f "$CRAWL4AI_INSTALL_MARKER" ]]; then
    log "crawl4ai/playwright already installed ($CRAWL4AI_INSTALL_MARKER)"
    return 0
  fi
  command -v crawl4ai-setup >/dev/null 2>&1 \
    || die 'crawl4ai-setup not on PATH (bake crawl4ai into the image)'
  log 'installing playwright chromium'
  "$PYTHON" -m playwright install chromium
  log 'running crawl4ai-setup'
  crawl4ai-setup
  touch "$CRAWL4AI_INSTALL_MARKER"
}

install_deps() {
  install_python_deps
  install_node_deps
  install_crawl4ai_browsers
}

ensure_file() {
  local dest="$1"
  shift
  if [[ -f "$dest" ]]; then
    log "model present: $dest"
    return 0
  fi
  local repo="$1"
  shift
  local dir
  dir="$(dirname "$dest")"
  mkdir -p "$dir"
  log "downloading into $dir from $repo: $*"
  hf download "$repo" "$@" --local-dir "$dir"
  [[ -f "$dest" ]] || die "download finished but missing: $dest"
}

hf_auth() {
  require_hf_token
  log 'authenticating with Hugging Face Hub'
  hf auth login --token "$HF_TOKEN"
}

download_models() {
  hf_auth
  mkdir -p "$MODELS_ROOT" "$HF_HOME"

  # Honor decrypted .env for whisper path; default under MODELS_ROOT.
  if [[ -z "${WHISPER_MODEL_PATH:-}" ]]; then
    export WHISPER_MODEL_PATH="$MODELS_ROOT/whisper/ggml-small.bin"
  fi

  ensure_file \
    "$MODELS_ROOT/qwen3.6-27b/Qwen3.6-27B-Q8_0.gguf" \
    unsloth/Qwen3.6-27B-MTP-GGUF \
    Qwen3.6-27B-Q8_0.gguf

  ensure_file \
    "$MODELS_ROOT/nanonets-ocr2-3b/Nanonets-OCR2-3B.Q8_0.gguf" \
    mradermacher/Nanonets-OCR2-3B-GGUF \
    Nanonets-OCR2-3B.Q8_0.gguf

  ensure_file \
    "$MODELS_ROOT/nanonets-ocr2-3b/Nanonets-OCR2-3B.mmproj-Q8_0.gguf" \
    mradermacher/Nanonets-OCR2-3B-GGUF \
    Nanonets-OCR2-3B.mmproj-Q8_0.gguf

  ensure_file \
    "$MODELS_ROOT/qwen3.5-9b/Qwen3.5-9B-Q4_0.gguf" \
    unsloth/Qwen3.5-9B-GGUF \
    Qwen3.5-9B-Q4_0.gguf

  # Whisper: download to the path the pipeline expects (from .env).
  ensure_file \
    "$WHISPER_MODEL_PATH" \
    ggerganov/whisper.cpp \
    ggml-small.bin
}

configure_whisper_paths() {
  [[ -n "${WHISPER_CPP_BIN:-}" ]] || die 'WHISPER_CPP_BIN unset (.env must define it, e.g. /usr/local/bin/whisper-cli)'
  [[ -n "${WHISPER_MODEL_PATH:-}" ]] || die 'WHISPER_MODEL_PATH unset (.env must define it)'
  export WHISPER_MODEL_PATH WHISPER_CPP_BIN
  [[ -x "$WHISPER_CPP_BIN" ]] || die "whisper binary not executable: $WHISPER_CPP_BIN"
  [[ -f "$WHISPER_MODEL_PATH" ]] || die "whisper model not found: $WHISPER_MODEL_PATH"
  log "whisper: $WHISPER_CPP_BIN -m $WHISPER_MODEL_PATH"
}

cleanup_llama_servers() {
  local pid
  for pid in "${LLAMA_PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  if [[ -n "${STATUS_RELAY_PID:-}" ]] && kill -0 "$STATUS_RELAY_PID" 2>/dev/null; then
    kill "$STATUS_RELAY_PID" 2>/dev/null || true
  fi
}

trap cleanup_llama_servers EXIT INT TERM

require_llama_server_bin() {
  [[ -x "$LLAMA_SERVER_BIN" ]] || die "llama-server not executable: $LLAMA_SERVER_BIN"
}

start_llama_servers() {
  require_llama_server_bin
  mkdir -p "$LOG_DIR"

  local llm_port vision_port tagger_port
  llm_port="$(port_from_url "${LLM_BASE_URL:-http://127.0.0.1:8000/v1}" 8000)"
  vision_port="$(port_from_url "${VISION_MODEL_URL:-http://127.0.0.1:8001/v1}" 8001)"
  tagger_port="$(port_from_url "${TAGGER_BASE_URL:-http://127.0.0.1:8002/v1}" 8002)"

  log "starting llama-server (main) on port $llm_port"
  # -fa: flash attention (Hopper-friendly, ~30% faster prompt eval + smaller KV cache).
  # --parallel 8: 8 concurrent slots (matches validators that fan out at CONCURRENCY=15).
  # -c 80000: total context, ~10k tokens/slot with --parallel 8 (headroom for long prompts).
  "$LLAMA_SERVER_BIN" \
    -m "$MODELS_ROOT/qwen3.6-27b/Qwen3.6-27B-Q8_0.gguf" \
    --n-gpu-layers 99 \
    -fa \
    --parallel 8 \
    -c 80000 \
    --port "$llm_port" \
    --chat-template-kwargs '{"enable_thinking":true}' \
    >"$LOG_DIR/llama-main.log" 2>&1 &
  LLAMA_PIDS+=("$!")

  log "starting llama-server (vision) on port $vision_port"
  # Note: deliberately NOT passing -fa here. Flash attention with mmproj vision
  # towers has historically been buggy in llama.cpp; vision isn't a bottleneck,
  # not worth the risk. Re-add only after verifying OCR quality is unaffected.
  "$LLAMA_SERVER_BIN" \
    -m "$MODELS_ROOT/nanonets-ocr2-3b/Nanonets-OCR2-3B.Q8_0.gguf" \
    --mmproj "$MODELS_ROOT/nanonets-ocr2-3b/Nanonets-OCR2-3B.mmproj-Q8_0.gguf" \
    --n-gpu-layers 99 \
    --port "$vision_port" \
    >"$LOG_DIR/llama-vision.log" 2>&1 &
  LLAMA_PIDS+=("$!")

  log "starting llama-server (tagger) on port $tagger_port"
  "$LLAMA_SERVER_BIN" \
    --model "$MODELS_ROOT/qwen3.5-9b/Qwen3.5-9B-Q4_0.gguf" \
    --ctx-size 32000 \
    --n-gpu-layers 99 \
    -fa \
    --port "$tagger_port" \
    >"$LOG_DIR/llama-tagger.log" 2>&1 &
  LLAMA_PIDS+=("$!")
}

wait_for_openai() {
  local base_url="$1"
  local log_file="$2"
  local label="$3"
  local url="${base_url%/v1}/v1/models"
  local attempt=1

  log "waiting for $label at $url"
  while (( attempt <= LLAMA_WAIT_ATTEMPTS )); do
    if curl -sf --connect-timeout 5 "$url" >/dev/null 2>&1; then
      log "$label is ready"
      return 0
    fi
    if (( attempt == 1 || attempt % 6 == 0 )); then
      log "$label not ready (attempt $attempt/$LLAMA_WAIT_ATTEMPTS)"
    fi
    sleep "$LLAMA_WAIT_INTERVAL_SEC"
    (( attempt++ )) || true
  done

  printf '[entrypoint] error: timeout waiting for %s (%s)\n' "$label" "$url" >&2
  if [[ -f "$log_file" ]]; then
    printf '[entrypoint] last 20 lines of %s:\n' "$log_file" >&2
    tail -n 20 "$log_file" >&2 || true
  fi
  return 1
}

wait_for_all_llama_servers() {
  wait_for_openai "${LLM_BASE_URL:-http://127.0.0.1:8000/v1}" "$LOG_DIR/llama-main.log" 'main LLM' &
  local p_main=$!
  wait_for_openai "${VISION_MODEL_URL:-http://127.0.0.1:8001/v1}" "$LOG_DIR/llama-vision.log" 'vision LLM' &
  local p_vision=$!
  wait_for_openai "${TAGGER_BASE_URL:-http://127.0.0.1:8002/v1}" "$LOG_DIR/llama-tagger.log" 'tagger LLM' &
  local p_tagger=$!

  local failed=0
  wait "$p_main" || failed=1
  wait "$p_vision" || failed=1
  wait "$p_tagger" || failed=1
  (( failed == 0 )) || die 'one or more llama-server instances failed health check'
}

ORCHESTRATE_LOG="$LOG_DIR/orchestrate.log"
ORCHESTRATE_PID_FILE="$LOG_DIR/orchestrate.pid"
ORCHESTRATE_EXIT_FILE="$LOG_DIR/orchestrate.exit"

start_status_relay() {
  if [[ "${STATUS_RELAY_ENABLED:-1}" == "0" ]]; then
    log 'status relay disabled (STATUS_RELAY_ENABLED=0)'
    return 0
  fi
  log 'starting status relay'
  "$PYTHON" "$REVIEW_GENERATOR_ROOT/status_relay.py" \
    --log-file "$ORCHESTRATE_LOG" \
    --pid-file "$ORCHESTRATE_PID_FILE" \
    --exit-file "$ORCHESTRATE_EXIT_FILE" &
  STATUS_RELAY_PID=$!
}

run_orchestrate() {
  log "running orchestrate.py $*"
  # Do not exec: background llama-server PIDs must be reaped on exit (EXIT trap).
  mkdir -p "$LOG_DIR"
  : >"$ORCHESTRATE_LOG"
  rm -f "$ORCHESTRATE_PID_FILE" "$ORCHESTRATE_EXIT_FILE"
  start_status_relay
  (
    cd "$REVIEW_GENERATOR_ROOT"
    exec "$PYTHON" "$REVIEW_GENERATOR_ROOT/orchestrate.py" "$@"
  ) > >(tee "$ORCHESTRATE_LOG") 2>&1 &
  local orch_pid=$!
  echo "$orch_pid" >"$ORCHESTRATE_PID_FILE"
  # Capture orchestrator exit code without tripping `set -e` on non-zero exits.
  local exit_code=0
  if ! wait "$orch_pid"; then
    exit_code=$?
  fi
  echo "$exit_code" >"$ORCHESTRATE_EXIT_FILE"
  if [[ -n "${STATUS_RELAY_PID:-}" ]]; then
    wait "$STATUS_RELAY_PID" 2>/dev/null || true
  fi
  return $exit_code
}

main() {
  log "review-generator root: $REVIEW_GENERATOR_ROOT"
  decrypt_secrets
  load_dotenv
  sync_hf_token_alias
  install_deps
  download_models
  configure_whisper_paths
  start_llama_servers
  wait_for_all_llama_servers
  run_orchestrate "$@"
}

main "$@"
