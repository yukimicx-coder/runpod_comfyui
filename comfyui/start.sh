#!/bin/bash
set -e # Exit the script if any statement returns a non-true return value

# ---------------------------------------------------------------------------- #
#                          Function Definitions                                #
# ---------------------------------------------------------------------------- #

# Setup ssh
setup_ssh() {
    if [[ $PUBLIC_KEY ]]; then
        echo "Setting up SSH..."
        echo "$PUBLIC_KEY" >> ~/.ssh/authorized_keys
        chmod 0600 ~/.ssh/authorized_keys

        ssh-keygen -A

        service ssh start

        echo "SSH host keys:"
        for key in /etc/ssh/*.pub; do
            echo "Key: $key"
            ssh-keygen -lf "$key"
        done
    fi
}

# Export env vars
export_env_vars() {
    echo "Exporting environment variables..."
    printenv | grep -E '^[A-Z_][A-Z0-9_]*=' | grep -v '^PUBLIC_KEY' | awk -F = '{ val = $0; sub(/^[^=]*=/, "", val); print "export " $1 "=\"" val "\"" }' > /etc/rp_environment
    if ! grep -q 'source /etc/rp_environment' ~/.bashrc; then
        echo 'source /etc/rp_environment' >> ~/.bashrc
    fi
}

prepare_comfyui() {
    if [ ! -d ~/comfy ]; then
        archive_file=/workspace/ComfyUI-latest.tar.gz
        if [ ! -f "$archive_file" ]; then
            echo "ComfyUI archive file missing"
            exit 1
        fi

        echo "extracting ComfyUI archive..."
        tar xzf "$archive_file" -C ~/

        if [ ! -f ~/comfy/ComfyUI/main.py ]; then
            echo "Failed to extract ComfyUI"
            exit 2
        fi
    fi

    _COMFY_ROOT=~/comfy/ComfyUI
    _MOUNT_ROOT=/workspace

    replace_dir_to_link() {
        if [ -d "$_COMFY_ROOT/$1" ]; then
            rm -rf "$_COMFY_ROOT/$1"
        fi
        ln -sf "$_MOUNT_ROOT/$1" "$_COMFY_ROOT/$1"
    }

    for dir in custom_nodes models output input user; do
        replace_dir_to_link $dir
    done

    echo "update dependecies..."
    pushd ~/comfy
    uv run comfy --here --where local --skip-prompt install --restore --fast-deps --nvidia --cuda-version 13.0
    popd


}
# ---------------------------------------------------------------------------- #
#                               Main Program                                   #
# ---------------------------------------------------------------------------- #

echo "Pod Started"

setup_ssh
export_env_vars

echo "Start script(s) finished, Pod is ready to use."

prepare_comfyui

comfyui_launcher.sh

if [ -x /post_start.sh ]; then
    /post_start.sh
fi

exec tail -F /root/comfy/ComfyUI/user/comfyui_8188.log
