import base64
import json
import os
import uuid
from pathlib import Path

import requests
import runpod
import websocket


COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
WORKFLOW_PATH = os.getenv("WORKFLOW_PATH", "/workflow_api.json")
INPUT_DIR = os.getenv("COMFYUI_INPUT_DIR", "/comfyui/input")
OUTPUT_DIR = os.getenv("COMFYUI_OUTPUT_DIR", "/comfyui/output")
TEMP_DIR = os.getenv("COMFYUI_TEMP_DIR", "/comfyui/temp")
PROMPT_NODE_ID = os.getenv("PROMPT_NODE_ID", "6")
SOURCE_IMAGE_NODE_ID = os.getenv("SOURCE_IMAGE_NODE_ID", "17")
REQUEST_TIMEOUT = int(os.getenv("COMFYUI_REQUEST_TIMEOUT", "30"))
WEBSOCKET_TIMEOUT = int(os.getenv("COMFYUI_WEBSOCKET_TIMEOUT", "900"))
MAX_SOURCE_IMAGE_BYTES = int(os.getenv("MAX_SOURCE_IMAGE_BYTES", str(12 * 1024 * 1024)))

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "high quality, detailed, photorealistic, natural lighting",
)


def load_workflow():
    with open(WORKFLOW_PATH, "r") as f:
        return json.load(f)


def validate_workflow(workflow):
    prompt_node = workflow.get(PROMPT_NODE_ID)
    if not prompt_node or prompt_node.get("class_type") != "CLIPTextEncode":
        raise ValueError(f"Prompt node {PROMPT_NODE_ID!r} is missing or is not CLIPTextEncode")

    source_node = workflow.get(SOURCE_IMAGE_NODE_ID)
    if not source_node or source_node.get("class_type") != "LoadImage":
        raise ValueError(f"Source image node {SOURCE_IMAGE_NODE_ID!r} is missing or is not LoadImage")


def update_workflow_prompt(workflow, user_prompt):
    prompt_node = workflow.get(PROMPT_NODE_ID)
    if not prompt_node or prompt_node.get("class_type") != "CLIPTextEncode":
        raise ValueError(f"Prompt node {PROMPT_NODE_ID!r} is missing or is not CLIPTextEncode")

    prompt_parts = [SYSTEM_PROMPT.strip(), user_prompt.strip()]
    prompt_node["inputs"]["text"] = ", ".join(part for part in prompt_parts if part)
    return workflow


def update_workflow_source(workflow, source_filename):
    source_node = workflow.get(SOURCE_IMAGE_NODE_ID)
    if not source_node or source_node.get("class_type") != "LoadImage":
        raise ValueError(f"Source image node {SOURCE_IMAGE_NODE_ID!r} is missing or is not LoadImage")

    source_node["inputs"]["image"] = source_filename
    return workflow


def queue_prompt(workflow, client_id):
    response = requests.post(
        f"{COMFYUI_URL}/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=REQUEST_TIMEOUT,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(f"ComfyUI /prompt rejected workflow: {response.text}") from exc
    data = response.json()

    if "prompt_id" not in data:
        raise RuntimeError(f"ComfyUI error: {data}")

    return data["prompt_id"]


def wait_for_completion(prompt_id, client_id):
    ws_url = COMFYUI_URL.replace("http://", "ws://").replace("https://", "wss://")
    ws = websocket.WebSocket(timeout=WEBSOCKET_TIMEOUT)
    try:
        ws.connect(f"{ws_url}/ws?clientId={client_id}", timeout=REQUEST_TIMEOUT)
        while True:
            raw_message = ws.recv()
            if isinstance(raw_message, bytes):
                continue

            message = json.loads(raw_message)
            message_type = message.get("type")
            data = message.get("data", {})

            if message_type == "execution_error" and data.get("prompt_id") == prompt_id:
                node_id = data.get("node_id", "unknown")
                exception = data.get("exception_message") or data.get("exception_type") or "unknown error"
                raise RuntimeError(f"ComfyUI execution failed at node {node_id}: {exception}")

            if message_type == "execution_interrupted" and data.get("prompt_id") == prompt_id:
                raise RuntimeError("ComfyUI execution was interrupted")

            if (
                message_type == "executing"
                and data.get("node") is None
                and data.get("prompt_id") == prompt_id
            ):
                return
    finally:
        ws.close()


def get_history(prompt_id):
    response = requests.get(f"{COMFYUI_URL}/history/{prompt_id}", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json().get(prompt_id, {})


def get_output_images(prompt_id):
    history = get_history(prompt_id)
    status = history.get("status", {})
    messages = status.get("messages", [])
    for level, message in messages:
        if level == "execution_error":
            node_id = message.get("node_id", "unknown")
            exception = message.get("exception_message") or message.get("exception_type") or "unknown error"
            raise RuntimeError(f"ComfyUI execution failed at node {node_id}: {exception}")

    outputs = history.get("outputs", {})
    images = []
    for node_output in outputs.values():
        images.extend(node_output.get("images", []))
    return images


def run_workflow(workflow):
    client_id = str(uuid.uuid4())
    prompt_id = queue_prompt(workflow, client_id)
    wait_for_completion(prompt_id, client_id)
    output_images = get_output_images(prompt_id)

    result_paths = []
    for img in output_images:
        path = resolve_output_path(img)
        if path and path.exists():
            result_paths.append(str(path))

    return result_paths


def resolve_output_path(image_info):
    filename = image_info.get("filename")
    if not filename:
        return None

    folder_type = image_info.get("type", "output")
    base_dir = TEMP_DIR if folder_type == "temp" else OUTPUT_DIR
    subfolder = image_info.get("subfolder") or ""

    base_path = Path(base_dir).resolve()
    path = (base_path / subfolder / filename).resolve()
    try:
        path.relative_to(base_path)
    except ValueError:
        raise ValueError(f"Unsafe output path returned by ComfyUI: {path}")

    return path


def strip_data_uri_prefix(source_image):
    if "," in source_image and source_image.strip().lower().startswith("data:"):
        return source_image.split(",", 1)[1]
    return source_image


def detect_image_extension(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def save_source_image(source_image, task_id):
    if source_image.startswith("http"):
        response = requests.get(source_image, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        content = response.content
    else:
        content = base64.b64decode(strip_data_uri_prefix(source_image), validate=True)

    if len(content) > MAX_SOURCE_IMAGE_BYTES:
        raise ValueError("Source image is too large")

    os.makedirs(INPUT_DIR, exist_ok=True)
    source_path = f"{INPUT_DIR}/source_{task_id}{detect_image_extension(content)}"
    with open(source_path, "wb") as f:
        f.write(content)

    return source_path


def handler(job):
    job_input = job.get("input", {})
    if not isinstance(job_input, dict):
        return {"error": "Input must be a JSON object"}

    user_prompt = str(job_input.get("prompt", "")).strip()
    if not user_prompt:
        return {"error": "Field 'prompt' is required"}

    source_image = job_input.get("source_image")
    task_id = str(uuid.uuid4())[:8]
    source_path = None
    result_paths = []

    try:
        if source_image:
            source_path = save_source_image(str(source_image), task_id)

        workflow = load_workflow()
        validate_workflow(workflow)
        workflow = update_workflow_prompt(workflow, user_prompt)

        if source_path:
            workflow = update_workflow_source(workflow, os.path.basename(source_path))

        result_paths = run_workflow(workflow)

        if not result_paths:
            return {"error": "Generation failed: no output images found"}

        output = []
        for path in result_paths:
            with open(path, "rb") as f:
                output.append(base64.b64encode(f.read()).decode())

        return {"images": output}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        if source_path and os.path.exists(source_path):
            try:
                os.remove(source_path)
            except OSError:
                pass
        for path in result_paths:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
