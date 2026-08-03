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


# ---------------------------------------------------------------------------- #
#                               Main Program                                   #
# ---------------------------------------------------------------------------- #

echo "Pod Started"

setup_ssh
export_env_vars

echo "Start script(s) finished, Pod is ready to use."

if [ -z "$NO_COMFYUI" ]; then
    ~/comfy/setup_comfyui.sh
    comfyui_launcher.sh
fi

# end of 'set -e'
set +e 

if [ -f "/workspace/post_start.sh" ]; then
    bash "/workspace/post_start.sh"
fi

if [ -n "$MODEL_DL_AUTO_START" ]; then
    run_download.sh &
fi

exec tail -F /root/comfy/ComfyUI/user/comfyui_8188.log
