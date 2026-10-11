"""Download pinned v1.0 CPU model and matching voice pack at build time."""
import os
import urllib.request
from pathlib import Path

BASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"


def download_file(filename: str, directory: Path) -> None:
    destination = directory / filename
    if destination.is_file() and destination.stat().st_size:
        return
    temporary = destination.with_suffix(destination.suffix + ".part")
    urllib.request.urlretrieve(f"{BASE_URL}/{filename}", temporary)
    temporary.replace(destination)
    print(f"Downloaded {filename}: {destination.stat().st_size} bytes", flush=True)


if __name__ == "__main__":
    directory = Path(os.environ.get("KOKORO_MODEL_DIR", "/app/models"))
    directory.mkdir(parents=True, exist_ok=True)
    for filename in ("kokoro-v1.0.int8.onnx", "voices-v1.0.bin"):
        download_file(filename, directory)
