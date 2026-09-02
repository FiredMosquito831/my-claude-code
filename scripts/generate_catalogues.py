"""Write every harness catalogue from a captured payload, for CLI validation.

Not part of the product. It exists so the real-CLI matrix can be re-run without
a server: it takes the ``models`` array of a ``GET /admin/api/catalogue-models``
capture (or the vendored fixture), runs every serialiser exactly as the server
would, and writes each document under a scratch directory at the filename the
harness registry declares for it.

    uv run --offline python scripts/generate_catalogues.py <out_dir> [payload.json]

Placeholders (``{env:...}``, ``$VAR``) are left exactly as the serialiser wrote
them: the point is to validate the document a CLI is handed, and every CLI
expands them itself.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from my_claude_code.application.catalogue_model import CatalogueModel  # noqa: E402
from my_claude_code.application.catalogues import (  # noqa: E402
    model_entries,
    serialise,
    serialise_sidecar,
)
from my_claude_code.config.harness_toml import toml_document_bytes  # noqa: E402
from my_claude_code.config.harnesses import harness_specs  # noqa: E402
from tests.fixtures.live_catalogue import _record  # noqa: E402


def _models(payload_path: Path | None) -> tuple[CatalogueModel, ...]:
    if payload_path is None:
        from tests.fixtures.live_catalogue import live_catalogue_models

        return live_catalogue_models()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    return tuple(_record(entry) for entry in payload["models"])


def main() -> int:
    out_dir = Path(sys.argv[1])
    payload = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    out_dir.mkdir(parents=True, exist_ok=True)
    models = _models(payload)

    for spec in harness_specs():
        catalogue = spec.catalogue
        if catalogue is None or catalogue.filename is None:
            continue
        document, defaulted = serialise(catalogue.format_id, models)
        path = out_dir / catalogue.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if catalogue.document_format == "toml":
            path.write_bytes(toml_document_bytes(document))
        else:
            path.write_text(
                json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
        sidecar = serialise_sidecar(catalogue.format_id, models)
        if sidecar is not None and catalogue.sidecar_filename:
            (out_dir / catalogue.sidecar_filename).write_text(
                json.dumps(sidecar, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
        print(
            f"{spec.id:16s} {catalogue.format_id:12s} "
            f"models={len(model_entries(catalogue.format_id, document)):4d} "
            f"defaulted={defaulted.model_count:4d} {path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
