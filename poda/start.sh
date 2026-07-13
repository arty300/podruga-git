#!/bin/bash
set -euo pipefail

echo "=== RunPod worker start.sh ==="
echo "IMAGE_VERSION=${IMAGE_VERSION:-unknown}"

NETWORK_COMFYUI_DIR="${NETWORK_COMFYUI_DIR:-}"
ALLOW_RUNTIME_PIP_INSTALL="${ALLOW_RUNTIME_PIP_INSTALL:-0}"
COPY_NETWORK_VOLUME="${COPY_NETWORK_VOLUME:-0}"
INSTALL_CUSTOM_NODE_REQUIREMENTS="${INSTALL_CUSTOM_NODE_REQUIREMENTS:-1}"
BASE_PYTHON_IMPORTS="sqlalchemy alembic uvicorn filelock runpod requests websocket"
CUSTOM_NODE_PYTHON_IMPORTS="blend_modes segment_anything insightface onnxruntime ultralytics dill numba facexlib piexif skimage cv2 openai diffusers accelerate peft transformers"

copy_if_exists() {
    local source_dir="$1"
    local target_dir="$2"
    local label="$3"

    echo "=== Copying ${label} ==="
    if [ -d "$source_dir" ]; then
        mkdir -p "$target_dir"
        cp -rn "$source_dir"/* "$target_dir"/ 2>/dev/null || true
        echo "${label} copied"
    else
        echo "WARNING: ${label} not found at ${source_dir}"
    fi
}

link_or_copy_if_exists() {
    local source_dir="$1"
    local target_dir="$2"
    local label="$3"

    echo "=== Preparing ${label} ==="
    if [ ! -d "$source_dir" ]; then
        echo "WARNING: ${label} not found at ${source_dir}"
        return
    fi

    if [ "$COPY_NETWORK_VOLUME" = "1" ]; then
        copy_if_exists "$source_dir" "$target_dir" "$label"
        return
    fi

    rm -rf "$target_dir"
    ln -s "$source_dir" "$target_dir"
    echo "${label} linked: ${target_dir} -> ${source_dir}"
}

detect_network_comfyui_dir() {
    if [ -n "$NETWORK_COMFYUI_DIR" ]; then
        echo "$NETWORK_COMFYUI_DIR"
        return
    fi

    for candidate in \
        /workspace/ComfyUI \
        /runpod-volume/ComfyUI \
        /workspace \
        /runpod-volume
    do
        if [ -d "${candidate}/models" ] || [ -d "${candidate}/custom_nodes" ] || [ -d "${candidate}/input" ]; then
            echo "$candidate"
            return
        fi
    done

    echo "/workspace/ComfyUI"
}

print_volume_diagnostics() {
    echo "=== Volume diagnostics ==="
    echo "/workspace:"
    ls -la /workspace 2>/dev/null | head -20 || true
    echo "/runpod-volume:"
    ls -la /runpod-volume 2>/dev/null | head -20 || true
    echo "${NETWORK_COMFYUI_DIR}:"
    ls -la "$NETWORK_COMFYUI_DIR" 2>/dev/null | head -20 || true
}

install_custom_node_requirements() {
    if [ "$INSTALL_CUSTOM_NODE_REQUIREMENTS" != "1" ]; then
        echo "Skipping persistent custom node requirements install"
        return
    fi

    echo "=== Installing persistent custom node requirements ==="
    mkdir -p "$PYTHON_DEPS_DIR"

    local requirements_list="/tmp/custom-node-requirements-files.txt"
    : > "$requirements_list"
    if [ -f /opt/custom-node-requirements.txt ]; then
        echo "/opt/custom-node-requirements.txt" >> "$requirements_list"
    fi
    find /comfyui/custom_nodes -mindepth 2 -maxdepth 2 -name requirements.txt -print 2>/dev/null | sort >> "$requirements_list"

    if [ ! -s "$requirements_list" ]; then
        echo "No custom node requirements found"
        return
    fi

    echo "Requirement files:"
    sed 's/^/  - /' "$requirements_list"

    local requirements_hash
    requirements_hash="$(while IFS= read -r requirements_file; do sha256sum "$requirements_file"; done < "$requirements_list" | sha256sum | awk '{print $1}')"
    local marker_file="${PYTHON_DEPS_DIR}/.custom_node_requirements.sha256"

    if [ "${FORCE_INSTALL_CUSTOM_NODE_REQUIREMENTS:-0}" != "1" ] && [ -f "$marker_file" ] && [ "$(cat "$marker_file")" = "$requirements_hash" ]; then
        echo "Persistent custom node requirements already installed: ${requirements_hash}"
        return
    fi

    while IFS= read -r requirements_file; do
        echo "Installing ${requirements_file} into ${PYTHON_DEPS_DIR}"
        python3 -m pip install \
            --upgrade \
            --target "$PYTHON_DEPS_DIR" \
            -r "$requirements_file" \
            -c /tmp/torch-cu121-constraints.txt \
            --extra-index-url https://download.pytorch.org/whl/cu121
    done < "$requirements_list"

    echo "Ensuring opencv contrib package is first on PYTHONPATH"
    python3 -m pip install \
        --upgrade \
        --force-reinstall \
        --target "$PYTHON_DEPS_DIR" \
        "opencv-contrib-python-headless<4.12" \
        -c /tmp/torch-cu121-constraints.txt

    echo "$requirements_hash" > "$marker_file"
    echo "Persistent custom node requirements installed: ${requirements_hash}"
}

configure_persistent_python_deps() {
    PYTHON_DEPS_DIR="${PYTHON_DEPS_DIR:-${NETWORK_COMFYUI_DIR}/python_deps/py310}"
    mkdir -p "$PYTHON_DEPS_DIR"
    export PYTHONPATH="${PYTHON_DEPS_DIR}:${PYTHONPATH:-}"
    export PATH="${PYTHON_DEPS_DIR}/bin:${PATH}"
    echo "=== Persistent Python deps ==="
    echo "PYTHON_DEPS_DIR=${PYTHON_DEPS_DIR}"
}

check_python_imports() {
    python3 - "$@" <<'PY'
import importlib
import sys

missing = []
for module_name in sys.argv[1:]:
    try:
        importlib.import_module(module_name)
    except Exception as exc:
        missing.append(f"{module_name}: {type(exc).__name__}: {exc}")

if missing:
    print("Missing or broken Python imports:")
    for item in missing:
        print(f"  - {item}")
    raise SystemExit(1)

try:
    import cv2
    assert hasattr(cv2, "ximgproc") and hasattr(cv2.ximgproc, "guidedFilter")
except Exception as exc:
    print(f"Missing or broken Python imports:\n  - cv2.ximgproc.guidedFilter: {type(exc).__name__}: {exc}")
    raise SystemExit(1)

PY
}

configure_python_cuda_library_path() {
    echo "=== Configuring Python CUDA library path ==="
    PY_SITE_PACKAGES="$(python3 -c 'import site; print(site.getsitepackages()[0])')"
    PY_CUDA_LIBS=""

    for lib_dir in \
        "${PY_SITE_PACKAGES}/nvidia/nccl/lib" \
        "${PY_SITE_PACKAGES}/nvidia/cublas/lib" \
        "${PY_SITE_PACKAGES}/nvidia/cuda_runtime/lib" \
        "${PY_SITE_PACKAGES}/nvidia/cuda_nvrtc/lib" \
        "${PY_SITE_PACKAGES}/nvidia/cuda_cupti/lib" \
        "${PY_SITE_PACKAGES}/nvidia/cudnn/lib" \
        "${PY_SITE_PACKAGES}/nvidia/cufft/lib" \
        "${PY_SITE_PACKAGES}/nvidia/curand/lib" \
        "${PY_SITE_PACKAGES}/nvidia/cusolver/lib" \
        "${PY_SITE_PACKAGES}/nvidia/cusparse/lib"
    do
        if [ -d "$lib_dir" ]; then
            PY_CUDA_LIBS="${PY_CUDA_LIBS:+${PY_CUDA_LIBS}:}${lib_dir}"
        fi
    done

    if [ -n "$PY_CUDA_LIBS" ]; then
        export LD_LIBRARY_PATH="${PY_CUDA_LIBS}:${LD_LIBRARY_PATH:-}"
        echo "Python CUDA libs added to LD_LIBRARY_PATH"
    else
        echo "WARNING: No Python CUDA library directories found under ${PY_SITE_PACKAGES}/nvidia"
    fi
}

ensure_torch_cuda121() {
    echo "=== Checking PyTorch CUDA build ==="
    if python3 -c "import torch; assert torch.version.cuda and torch.version.cuda.startswith('12.1'), torch.version.cuda" >/dev/null 2>&1; then
        python3 -c "import torch; print(f'PyTorch CUDA build ready: torch={torch.__version__}, cuda={torch.version.cuda}')"
        return
    fi

    if [ "$ALLOW_RUNTIME_PIP_INSTALL" != "1" ]; then
        echo "ERROR: PyTorch is not built for CUDA 12.1. Rebuild the image; runtime pip install is disabled."
        exit 1
    fi

    echo "WARNING: PyTorch is not built for CUDA 12.1; reinstalling torch/torchvision/torchaudio cu121"
    python3 -m pip install --force-reinstall torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
    python3 -c "import torch; print(f'PyTorch CUDA build ready: torch={torch.__version__}, cuda={torch.version.cuda}'); assert torch.version.cuda and torch.version.cuda.startswith('12.1'), torch.version.cuda"
}

ensure_runtime_dependencies() {
    echo "=== Checking Python runtime dependencies ==="
    if check_python_imports $BASE_PYTHON_IMPORTS $CUSTOM_NODE_PYTHON_IMPORTS; then
        echo "Python runtime and custom node dependencies ready"
        return
    fi

    if [ "$ALLOW_RUNTIME_PIP_INSTALL" != "1" ]; then
        echo "ERROR: Python runtime or custom node dependencies are missing. Rebuild the base image; runtime pip install is disabled."
        exit 1
    fi

    echo "WARNING: Python runtime or custom node dependencies missing; installing requirements"
    python3 -m pip install -r /comfyui/requirements.txt -c /tmp/torch-cu121-constraints.txt --extra-index-url https://download.pytorch.org/whl/cu121
    python3 -m pip install "SQLAlchemy>=2.0.0" alembic uvicorn filelock runpod requests websocket-client
    if [ -f /opt/custom-node-requirements.txt ]; then
        python3 -m pip install -r /opt/custom-node-requirements.txt -c /tmp/torch-cu121-constraints.txt --extra-index-url https://download.pytorch.org/whl/cu121
        python3 -m pip install --force-reinstall "opencv-contrib-python-headless<4.12" -c /tmp/torch-cu121-constraints.txt
    else
        echo "WARNING: /opt/custom-node-requirements.txt not found; custom node dependencies were not installed"
    fi
    ensure_torch_cuda121
    check_python_imports $BASE_PYTHON_IMPORTS $CUSTOM_NODE_PYTHON_IMPORTS
}

NETWORK_COMFYUI_DIR="$(detect_network_comfyui_dir)"

echo "=== Checking Network Volume ==="
echo "NETWORK_COMFYUI_DIR=${NETWORK_COMFYUI_DIR}"
print_volume_diagnostics

link_or_copy_if_exists "${NETWORK_COMFYUI_DIR}/models" "/comfyui/models" "models"
link_or_copy_if_exists "${NETWORK_COMFYUI_DIR}/custom_nodes" "/comfyui/custom_nodes" "custom nodes"
link_or_copy_if_exists "${NETWORK_COMFYUI_DIR}/input" "/comfyui/input" "input assets"
if [ -e "/comfyui/custom_nodes/*" ]; then
    echo "Removing stray custom node wildcard path"
    rm -rf "/comfyui/custom_nodes/*"
fi
mkdir -p /comfyui/output /comfyui/temp
configure_persistent_python_deps
install_custom_node_requirements
configure_python_cuda_library_path
ensure_runtime_dependencies
ensure_torch_cuda121

echo "=== Starting ComfyUI ==="
cd /comfyui || exit 1
python3 main.py --listen 127.0.0.1 --enable-cors-header --output-directory /comfyui/output &
COMFY_PID=$!

echo "Waiting for ComfyUI..."
COMFY_READY=0
for i in {1..60}; do
    if curl -fsS http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then
        echo "ComfyUI ready!"
        COMFY_READY=1
        break
    fi

    if ! kill -0 "$COMFY_PID" 2>/dev/null; then
        echo "ERROR: ComfyUI process exited before becoming ready"
        exit 1
    fi

    sleep 2
done

if [ "$COMFY_READY" != "1" ]; then
    echo "ERROR: ComfyUI did not become ready in time"
    exit 1
fi

echo "=== Starting Handler ==="
python3 /rp_handler.py
