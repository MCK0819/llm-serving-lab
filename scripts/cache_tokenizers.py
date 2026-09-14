"""Cache the two pinned tokenizer snapshots without downloading model weights.

Run from the repository root: python -m scripts.cache_tokenizers
The resulting directories work with AutoTokenizer.from_pretrained(path, local_files_only=True).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from huggingface_hub import snapshot_download

MODELS = {
    "e5": (
        "intfloat/multilingual-e5-small",
        "614241f622f53c4eeff9890bdc4f31cfecc418b3",
        (
            "config.json",
            "sentencepiece.bpe.model",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
        ),
    ),
    "qwen": (
        "Qwen/Qwen3-4B-Instruct-2507",
        "cdbee75f17c01a7cc42f958dc650907174af0554",
        (
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
        ),
    ),
}


def cache_model(name: str, root: Path) -> Path:
    repo_id, revision, filenames = MODELS[name]
    destination = root / name
    snapshot_download(
        repo_id=repo_id,
        revision=revision,
        local_dir=destination,
        allow_patterns=list(filenames),
        max_workers=4,
    )
    files = {}
    for filename in filenames:
        path = destination / filename
        if not path.is_file():
            raise RuntimeError(f"Tokenizer snapshot is incomplete: {name}/{filename}")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        files[filename] = {"sha256": digest, "bytes": path.stat().st_size}
    manifest = {"model": repo_id, "revision": revision, "files": files}
    (destination / "tokenizer-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return destination.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("e5", "qwen", "all"), default="all")
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1] / ".cache/tokenizers"
    )
    args = parser.parse_args()
    for name in MODELS if args.model == "all" else (args.model,):
        print(f"{name}: {cache_model(name, args.root)}")


if __name__ == "__main__":
    main()
