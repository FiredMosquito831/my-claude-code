"""tiktoken's ``cl100k_base`` for the suite, from the repository, not the network.

The token estimator (``core/token_encoder.py``) calls
``tiktoken.get_encoding("cl100k_base")``, and tiktoken fetches the BPE file
from ``openaipublic.blob.core.windows.net`` unless its cache already holds it.
A developer machine that once ran MCC has that cache in ``%TEMP%``, so a local
run never showed it; every CI runner starts empty, so every CI run downloaded
1.7 MB from Azure -- found by the network gate on its first CI run (PR #308:
1,934 refused lookups, 550 failures, 543 errors).

The file is kept here xz-compressed (561 KB) and unpacked once per machine into
a directory named after its hash, which ``TIKTOKEN_CACHE_DIR`` then names:
tiktoken finds it under the key it would have used (the sha1 of the URL) and
checks the sha256 it ships with, so a damaged copy fails loudly instead of being
fetched again. Child processes the suite starts inherit the variable.
Standard library only.
"""

import hashlib
import lzma
import os
import tempfile
import uuid
from pathlib import Path

#: Where tiktoken itself fetches the file from (its cache key is this URL's sha1).
CL100K_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
#: The sha256 ``tiktoken_ext.openai_public`` checks the file against.
CL100K_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
FIXTURE = Path(__file__).resolve().parent.parent / (
    "fixtures/tiktoken/cl100k_base.tiktoken.xz"
)


def install_offline_tokenizer() -> Path:
    """Unpack the fixture (once) and point ``TIKTOKEN_CACHE_DIR`` at it."""

    cache_dir = Path(tempfile.gettempdir()) / f"mcc-tests-tiktoken-{CL100K_SHA256[:16]}"
    target = cache_dir / hashlib.sha1(CL100K_URL.encode()).hexdigest()
    if not target.is_file() or (
        hashlib.sha256(target.read_bytes()).hexdigest() != CL100K_SHA256
    ):
        data = lzma.decompress(FIXTURE.read_bytes())
        if hashlib.sha256(data).hexdigest() != CL100K_SHA256:
            raise RuntimeError(f"{FIXTURE} is not tiktoken's cl100k_base file")
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Unique name, then an atomic replace: xdist workers unpack at once.
        partial = cache_dir / f".{uuid.uuid4().hex}.tmp"
        partial.write_bytes(data)
        try:
            os.replace(partial, target)
        except OSError:
            # Another worker won the race and holds the file open (Windows
            # refuses to replace an open file); its copy is the same bytes.
            partial.unlink(missing_ok=True)
            if hashlib.sha256(target.read_bytes()).hexdigest() != CL100K_SHA256:
                raise
    os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir)
    return cache_dir
