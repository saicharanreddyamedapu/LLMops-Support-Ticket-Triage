"""Install the pinned tokenizer tables before serving requests."""
import os
from pathlib import Path

import tiktoken


if __name__ == "__main__":
    cache = Path(os.getenv("CUSTOM_TIKTOKEN_CACHE_DIR", ".local/tokenizers")).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
    for encoding in ("cl100k_base", "o200k_base"):
        # tiktoken verifies each table against its pinned SHA-256.
        tiktoken.get_encoding(encoding)
    print("Tokenizer tables ready")
