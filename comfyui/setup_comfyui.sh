#/bin/bash

if [ -n "$SKIP_COMFYUI" ]; then
    exit 0
fi

_COMFYUI_ROOT=ComfyUI
_MOUNT_ROOT=/workspace

replace_subdir_to_symlink() {

    replace_dir_to_link() {
        [ ! -d "$_MOUNT_ROOT/$1" ] && return 0
        if [ -d "$_COMFY_ROOT/$1" ]; then
            rm -rf "$_COMFY_ROOT/$1"
        fi
        ln -sf "$_MOUNT_ROOT/$1" "$_COMFY_ROOT/$1"
        return 0
    }

    for dir in custom_nodes output input user/default models/checkpoints models/diffusion_models models/loras; do
        replace_dir_to_link $dir
    done
}

install_comfyui() {
    _restore_opt=
    if [ -e $_COMFY_ROOT ]; then
        echo "update dependecies..."
        _restore_opt=--restore
    else
        echo "installing ComfyUI"
    fi

    uv run comfy --here --where local --skip-prompt install $_restore_opt --fast-deps --nvidia \
            --cuda-version ${COMFYCLI_CUDA} --version ${COMFYCLI_TAG}
}

prepare_comfyui() {
    _comfyui_ver="v${COMFYCLI_TAG:=0.29.2}_cu${COMFYCLI_CUDA:=12.8}"
    echo "ComfyUI ver. $_comfyui_ver"

    if [ ! -d $_COMFYUI_ROOT ]; then
        archive_file=$_MOUNT_ROOT/ComfyUI_${_comfyui_ver}.tar.gz
        if [ -n "$USE_NETDRV_COMFYUI" ] && [ -d $_MOUNT_ROOT/ComfyUI ]; then
            echo "use $_MOUNT_ROOT/ComfyUI; symlinking..."
            ln -sf $_MOUNT_ROOT/ComfyUI $_COMFY_ROOT
        elif [ -f "$archive_file" ]; then
            echo "extracting archive..."
            tar xzf "$archive_file" -C ~/

            if [ ! -f $_COMFYUI_ROOT/main.py ] || [ ! -d $_COMFYUI_ROOT/custom_nodes ]; then
                echo "Failed to extract ComfyUI"
                echo "fall back normal install..."
                install_comfyui
            fi
            replace_subdir_to_symlink
        else
            install_comfyui
            replace_subdir_to_symlink
        fi
    fi
    install_comfyui
}

pushd ~/comfy

prepare_comfyui

popd

comfyui_launcher.sh restart

