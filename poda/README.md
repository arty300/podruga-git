# RunPod serverless worker

This folder is only for the RunPod serverless image generation worker.

Files:

- `Dockerfile`: builds the RunPod worker image.
- `start.sh`: copies models/custom nodes/input assets from the network volume, starts ComfyUI, then starts the RunPod handler.
- `rp_handler.py`: receives RunPod jobs, writes the prompt into ComfyUI workflow node `6`, keeps workflow node `17` as the static `LoadImage`, and returns base64 images.
- `elina_api.json`: ComfyUI API workflow. Do not edit it unless you intentionally change the generation graph.
- `custom-node-requirements.txt`: Python dependency bundle for custom nodes stored on the network volume.

## Worker input

The handler expects:

```json
{
  "input": {
    "prompt": "user prompt"
  }
}
```

`prompt` is appended to `SYSTEM_PROMPT` and written to workflow node `6`.
Node `17` is a static `LoadImage` node and always keeps the image filename from `elina_api.json`.
The Telegram bot intentionally sends text only; photos are ignored. Make sure this file exists on the network volume:

```text
<NETWORK_COMFYUI_DIR>/input/photo_2026-06-26_15-26-12.jpg
```

## Worker environment

Useful variables for the RunPod worker:

```bash
SYSTEM_PROMPT="high quality, detailed, photorealistic, natural lighting"
WORKFLOW_PATH=/workflow_api.json
COMFYUI_URL=http://127.0.0.1:8188
# Optional. If empty, start.sh tries /workspace/ComfyUI, /runpod-volume/ComfyUI, /workspace, /runpod-volume.
NETWORK_COMFYUI_DIR=/workspace/ComfyUI
PROMPT_NODE_ID=6
SOURCE_IMAGE_NODE_ID=17
COMFYUI_INPUT_DIR=/comfyui/input
COMFYUI_OUTPUT_DIR=/comfyui/output
COMFYUI_TEMP_DIR=/comfyui/temp
COMFYUI_REQUEST_TIMEOUT=30
COMFYUI_WEBSOCKET_TIMEOUT=900
COMFYUI_WEBSOCKET_CONNECT_TIMEOUT=30
USE_WORKFLOW_PROMPT_AS_SYSTEM_PROMPT=1
SYSTEM_PROMPT=
PROMPT_LOG_PREVIEW_CHARS=180
RANDOMIZE_SEEDS=1
MAX_SEED=9223372036854775807
OUTPUT_NODE_IDS=15
# Keep this disabled for fast cold starts. Use only for emergency debugging.
ALLOW_RUNTIME_PIP_INSTALL=0
# Default is symlinks from network volume. Set 1 only if you explicitly need copies.
COPY_NETWORK_VOLUME=0
```

By default, `start.sh` does not install Python dependencies into the network volume and does not add persistent Python folders to `PYTHONPATH`. Dependencies should be baked into the Docker base image for faster starts.

Emergency debug mode only:

```bash
USE_PERSISTENT_PYTHON_DEPS=1
INSTALL_CUSTOM_NODE_REQUIREMENTS=1
PYTHON_DEPS_DIR=/runpod-volume/ComfyUI/python_deps/py310
```

This mode can make startup much slower and can accidentally shadow packages from the image. Use it only when rebuilding the base image is temporarily impossible.

If startup logs show warnings like `models not found at /workspace/ComfyUI/models`, either the network volume is not mounted or its folder structure is different. Put assets under:

```text
/workspace/ComfyUI/models
/workspace/ComfyUI/custom_nodes
/workspace/ComfyUI/input
```

Or set `NETWORK_COMFYUI_DIR` to the real folder that contains `models`, `custom_nodes`, and `input`.

For example, if RunPod mounts the network volume at `/runpod-volume`, use one of these layouts:

```text
/runpod-volume/ComfyUI/models
/runpod-volume/ComfyUI/custom_nodes
/runpod-volume/ComfyUI/input
```

with:

```bash
NETWORK_COMFYUI_DIR=/runpod-volume/ComfyUI
```

or:

```text
/runpod-volume/models
/runpod-volume/custom_nodes
/runpod-volume/input
```

with:

```bash
NETWORK_COMFYUI_DIR=/runpod-volume
```

The startup logs include a `=== Volume diagnostics ===` section that prints the first entries of `/workspace`, `/runpod-volume`, and the selected `NETWORK_COMFYUI_DIR`.

## Cold start performance

The worker is optimized for fast cold starts:

- Python dependencies are installed during Docker build, not at container startup.
- Runtime `pip install` is disabled by default with `ALLOW_RUNTIME_PIP_INSTALL=0`.
- Network volume folders are symlinked into `/comfyui` by default instead of copied.

Avoid these settings unless debugging:

```bash
ALLOW_RUNTIME_PIP_INSTALL=1
COPY_NETWORK_VOLUME=1
FORCE_INSTALL_CUSTOM_NODE_REQUIREMENTS=1
USE_PERSISTENT_PYTHON_DEPS=1
INSTALL_CUSTOM_NODE_REQUIREMENTS=1
```

They can add minutes to cold start time.

## Build context

### Option A: GitHub Actions build

You can build without using the local machine. Push the project to GitHub, then open:

```text
Actions -> Build RunPod Images -> Run workflow
```

Create these GitHub repository secrets first:

```text
DOCKERHUB_USERNAME=drenk
DOCKERHUB_TOKEN=<Docker Hub access token>
```

Inputs:

```text
image_tag=v26
build_base=false
```

Use `build_base=true` when CUDA/PyTorch/ComfyUI/dependencies changed. For small worker-only changes, use `build_base=false`.

After the workflow finishes, set the RunPod image to:

```text
drenk/elina-generator:v26
```

### Option B: Local build

Build the heavy base image only when CUDA/PyTorch/ComfyUI/dependencies change:

```bash
cd poda
docker build -f Dockerfile.base -t drenk/elina-generator-base:cu121-comfyui .
docker push drenk/elina-generator-base:cu121-comfyui
```

Rebuild this base image when the custom nodes on the network volume change or logs show missing imports such as:

```text
ModuleNotFoundError: No module named 'ultralytics'
ModuleNotFoundError: No module named 'piexif'
ModuleNotFoundError: No module named 'segment_anything'
```

Then build the small deploy image when `rp_handler.py`, `start.sh`, or `elina_api.json` changes:

```bash
docker build -t drenk/elina-generator:v26 .
docker push drenk/elina-generator:v26
```

Do not use `--no-cache` for normal rebuilds. Use it only when the base image itself must be rebuilt from scratch:

```bash
docker build --no-cache -f Dockerfile.base -t drenk/elina-generator-base:cu121-comfyui .
```

## RunPod endpoint settings

Do not put `pip install ...` into the RunPod `Container Start Command`.
Dependencies are installed at image build time in the Dockerfile.

Recommended:

```text
Container Start Command: empty
```

If RunPod requires an explicit command, use:

```text
/start.sh
```

On a healthy container start, logs must include:

```text
=== RunPod worker start.sh ===
=== Checking Network Volume ===
=== Checking Python runtime dependencies ===
=== Checking PyTorch CUDA build ===
=== Configuring Python CUDA library path ===
=== Starting ComfyUI ===
```

If logs only show `Requirement already satisfied: sqlalchemy ...` and then the container restarts, RunPod is still overriding the container start command with a one-shot `pip install` command.
