import importlib


MODULES = [
    "torch",
    "sqlalchemy",
    "alembic",
    "uvicorn",
    "filelock",
    "runpod",
    "requests",
    "websocket",
    "blend_modes",
    "segment_anything",
    "insightface",
    "onnxruntime",
    "ultralytics",
    "dill",
    "numba",
    "facexlib",
    "piexif",
    "skimage",
    "cv2",
    "openai",
    "diffusers",
    "accelerate",
    "peft",
    "transformers",
]


def main():
    for module_name in MODULES:
        importlib.import_module(module_name)

    import cv2
    import torch

    if not hasattr(cv2, "ximgproc") or not hasattr(cv2.ximgproc, "guidedFilter"):
        raise RuntimeError("opencv-contrib-python-headless is required: cv2.ximgproc.guidedFilter is missing")

    if not torch.version.cuda or not torch.version.cuda.startswith("12.1"):
        raise RuntimeError(f"PyTorch must be built for CUDA 12.1, got {torch.version.cuda}")

    print(
        "Base imports ready: "
        f"torch={torch.__version__}, cuda={torch.version.cuda}, "
        f"cv2={cv2.__version__}"
    )


if __name__ == "__main__":
    main()
