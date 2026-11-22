#!/bin/sh
set -eu
: "${MODEL_PATH:?Set MODEL_PATH to the absolute path of a GGUF model}"
exec "${LLAMA_SERVER_BIN:-llama-server}" \
  --host "${LLAMA_HOST:-127.0.0.1}" --port "${LLAMA_PORT:-8080}" \
  --model "$MODEL_PATH" --alias "${LLAMA_MODEL_ALIAS:-team-local}" \
  --ctx-size "${LLAMA_CONTEXT_LENGTH:-16384}" --parallel "${LLAMA_PARALLEL:-1}" \
  --flash-attn "${LLAMA_FLASH_ATTENTION:-on}" \
  --cache-type-k "${LLAMA_CACHE_TYPE_K:-q8_0}" --cache-type-v "${LLAMA_CACHE_TYPE_V:-q8_0}" "$@"
