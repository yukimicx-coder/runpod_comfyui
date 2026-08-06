#/bin/bash

if [ -n "$SKIP_COMFYUI" ]; then
    exit 0
fi

_COMFYUI_ROOT=ComfyUI
_MOUNT_ROOT=/workspace

put_extra_model_paths() {
    cat <<EOL > $_COMFYUI_ROOT/extra_model_paths.yaml
workspace_models:
    base_path: $_MOUNT_ROOT/models/
    checkpoints: checkpoints/
    clip_vision: clip_vision/
    controlnet: controlnet/
    diffusion_models: diffusion_models/
    embeddings: embeddings/
    ipadapter: ipadapter/
    loras: loras/
    model_patches: model_patches/
    text_encoders: text_encoders/
    vae: vae/
EOL
}

replace_subdir_to_symlink() {

    replace_dir_to_link() {
        [ ! -d "$_MOUNT_ROOT/$1" ] && return 0
        if [ -d "$_COMFY_ROOT/$1" ]; then
            rm -rf "$_COMFY_ROOT/$1"
            ln -sf "$_MOUNT_ROOT/$1" "$_COMFY_ROOT/$1"
        fi
        return 0
    }

    if [ "$need_symlink_subdirs" -ne 0 ]; then
        for dir in custom_nodes models output input user; do
            replace_dir_to_link $dir
        done
    fi
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
                return 2
            fi
            replace_subdir_to_symlink
        else
            install_comfyui
            replace_subdir_to_symlink
        fi
    fi
    echo "update dependencies..."
    install_comfyui
}

pushd ~/comfy

prepare_comfyui

put_extra_model_paths 

popd

comfyui_launcher.sh restart

