#!/bin/sh
# Bootstrap an Ollama host on the Ubuntu Bot Team VM.
set -eu

deploy_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
app_dir=$(dirname "$deploy_dir")

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

free_kib() {
    df -Pk "$1" | awk 'NR == 2 {print $4}'
}

[ "$(id -u)" -eq 0 ] || fail "Run this setup with sudo on the Bot Team VM."
[ -x "$app_dir/.venv/bin/python" ] || fail "The Bot Team virtual environment is missing."
systemctl is-active --quiet botteam || fail "Start the Bot Team systemd service before running setup."

available_ram=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
[ "$available_ram" -ge 4194304 ] || fail "The VM needs at least 4 GiB of currently available RAM for this setup. Increase its VM memory and rerun."

temp_dir=$(mktemp -d)
trap 'rm -rf -- "$temp_dir"' EXIT

if ! command -v ollama >/dev/null 2>&1; then
    # Leave room for the runtime, the first model, and the OS.
    [ "$(free_kib /usr/local)" -ge 10485760 ] ||
        fail "Free at least 10 GiB on the filesystem containing /usr/local and rerun."
    if ! command -v zstd >/dev/null 2>&1; then
        apt-get update
        apt-get install -y zstd
    fi
    printf '%s\n' "Installing Ollama for Linux."
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --max-time 60 https://ollama.com/install.sh --output "$temp_dir/install.sh"
    sh "$temp_dir/install.sh"
fi

install -d /etc/systemd/system/ollama.service.d /etc/ollama
install -m 0644 "$deploy_dir/ollama.env" /etc/ollama/botteam.env
cat >"$temp_dir/botteam.conf" <<'EOF'
[Service]
EnvironmentFile=/etc/ollama/botteam.env
EOF
install -m 0644 "$temp_dir/botteam.conf" /etc/systemd/system/ollama.service.d/10-botteam.conf
systemctl daemon-reload
systemctl enable ollama
systemctl restart ollama

export OLLAMA_HOST=127.0.0.1:11434
attempts=0
until curl --noproxy '*' --fail --silent --max-time 2 "http://$OLLAMA_HOST/api/version" >/dev/null; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 30 ] ||
        fail "Ollama failed to start. Check: sudo journalctl -u ollama -n 40 --no-pager"
    sleep 1
done

# The official service stores model files under its Linux user's home.
model_home=$(getent passwd ollama | cut -d: -f6)
[ -d "$model_home" ] || fail "The Ollama system user's home directory is missing."

starter=qwen2.5-coder:3b
if [ "$available_ram" -ge 9437184 ] &&
    { ollama show qwen2.5-coder:7b >/dev/null 2>&1 || [ "$(free_kib "$model_home")" -ge 7340032 ]; }; then
    starter=qwen2.5-coder:7b
fi

if ! ollama show "$starter" >/dev/null 2>&1; then
    [ "$(free_kib "$model_home")" -ge 4194304 ] ||
        fail "Free at least 4 GiB on the model filesystem for the starter model and rerun."
    printf 'Downloading %s to the VM.\n' "$starter"
    ollama pull "$starter"
fi

"$app_dir/.venv/bin/python" "$deploy_dir/configure-version-pilot.py" --ollama "$starter"

general=qwen2.5:3b
if ! ollama show "$general" >/dev/null 2>&1; then
    if [ "$(free_kib "$model_home")" -ge 4194304 ]; then
        printf 'Downloading %s as an additional local model.\n' "$general"
        ollama pull "$general"
    else
        printf '%s\n' "Skipped the additional $general model. Free 4 GiB to add it with ollama pull $general."
    fi
fi
ollama list
