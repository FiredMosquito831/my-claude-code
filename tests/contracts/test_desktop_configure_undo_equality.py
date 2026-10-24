"""Configure and Undo give every desktop document what they always gave it (7.111.4).

ADDENDUM ONLY (user, 2026-10-24). 7.111.4 fixes two Codex shapes of the
Configure/Undo cycle and nothing else:

* **Shape 1 -- a root ``model`` / ``model_provider`` line that is not MCC's
  canonical ``key = "value"``**: a trailing comment, indentation, extra spaces
  around ``=``, a quoted key, a literal-string value. Until 7.111.4 Configure
  rewrote the whole line (``document_codecs._toml_set_root_scalar``) and Undo,
  in both modes, re-rendered it from the bare value the record kept, so the
  user's comment and spelling were gone after one cycle.
* **Shape 2 -- Undo "Remove MCC's keys" on a Codex that holds nothing of
  MCC's** (no ``[model_providers.mcc]`` and no live record): it deleted the
  user's own ``model`` and ``model_provider`` lines.

``desktop_configure_undo_baseline.json`` was captured from ``fork/main`` =
``c3597f9a`` (7.111.3) by running this very module there with
``MCC_DESKTOP_UNDO_BASELINE_WRITE`` set. Every key of it must be reproduced
byte for byte except the keys of those two shapes, which are listed below and
must each differ -- and each must now give the user's file back.

Two halves. The Codex half runs on every platform (``~/.codex/config.toml``
is the same path everywhere). Every other card's document lives where its app
keeps it on that platform, so that half is Windows' layout and runs on
Windows -- locally and in the Windows full-suite job -- like the 7.100.0 cycle
baseline beside it.

Pre-existing and deliberately NOT changed here (they are in the baseline and
reproduced as they are): a Codex document with no table header gains one
blank line through a cycle, one without a trailing newline gains one, and a
CRLF document comes back LF.
"""

import dataclasses
import hashlib
import json
import os
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.api.admin_desktop_app_routes import _documents
from my_claude_code.config import desktop_apply, paths
from my_claude_code.config.desktop_apply import (
    DesktopApplyError,
    document_path_for,
    sidecar_path_for,
)
from my_claude_code.config.desktop_apps import desktop_app
from my_claude_code.config.restore_record import UndoMode
from tests.config.test_desktop_apply import env_for, make_marker
from tests.contracts.test_mcp_media_off_is_byte_identical import (
    DESKTOP_SEEDS,
    _norm_escaped,
    _settings,
)
from tests.runtime.test_catalogue_fanout import FakeRuntime

BASELINE = Path(__file__).with_name("desktop_configure_undo_baseline.json")
WRITE_BASELINE = os.environ.get("MCC_DESKTOP_UNDO_BASELINE_WRITE", "")

CODEX = "codex_desktop"
_TABLE = '\n[profiles.fast]\nmodel = "x"\n'

#: Codex documents. The first five spell their root lines the way MCC renders
#: them, so nothing about them may change; the rest are shape 1.
CODEX_SEEDS: dict[str, str] = {
    "canonical": 'model = "gpt-5.6"\n' + _TABLE,
    "canonical-both": 'model_provider = "openai"\nmodel = "gpt-5.6"\n' + _TABLE,
    "canonical-root-only": 'model = "gpt-5.6"\n',
    "canonical-no-trailing-newline": 'model = "gpt-5.6"\n' + _TABLE.rstrip("\n"),
    "canonical-crlf": 'model = "gpt-5.6"\r\n\r\n[profiles.fast]\r\nmodel = "x"\r\n',
    "comment": '# mine\nmodel = "gpt-5.6"   # my default\n' + _TABLE,
    "tab-indent": '# mine\n\tmodel = "gpt-5.6"\t# tabbed\n' + _TABLE,
    "four-space": '    model  =  "gpt-5.6"    # spaced\n' + _TABLE,
    "quoted-key-literal": "\"model\" = 'gpt-5.6' # literal\n" + _TABLE,
    "hash-in-value": 'model = "a#b" # c\n' + _TABLE,
    "both-keys-comment": 'model_provider = "openai" # mine\nmodel = "gpt-5.6" # too\n'
    + _TABLE,
    "no-trailing-newline-comment": 'model = "gpt-5.6" # c\n' + _TABLE.rstrip("\n"),
    "crlf-comment": 'model = "gpt-5.6" # c\r\n\r\n[profiles.fast]\r\nmodel = "x"\r\n',
}

#: Shape 1: every seed whose root line is not MCC's canonical spelling.
SHAPE_ONE = frozenset(name for name in CODEX_SEEDS if not name.startswith("canonical"))

#: What the user's file is after each Undo on a shape-1 seed: the seed itself,
#: except where a pre-existing normalisation this release does not touch
#: applies (a cycle adds the missing trailing newline, and writes LF).
SHAPE_ONE_AFTER_UNDO: dict[str, str] = {
    name: CODEX_SEEDS[name] for name in SHAPE_ONE
} | {
    "no-trailing-newline-comment": CODEX_SEEDS["no-trailing-newline-comment"] + "\n",
    "crlf-comment": CODEX_SEEDS["crlf-comment"].replace("\r\n", "\n"),
}

#: Shape 2: the Codex seeds carrying a root ``model`` line of the user's own --
#: every one above -- which the old never-configured "Remove MCC's keys"
#: deleted. The card's own seed, the empty file and the missing file have no
#: such line and stay identical.
SHAPE_TWO = frozenset(CODEX_SEEDS)

#: The steps of one Configure / Undo cycle, as the dashboard offers them.
CYCLE = ("plan", "configure", "undo keys_only", "configure again", "undo restore")
#: Undo on a document MCC never configured, both modes.
NEVER = ("never-configured undo keys_only", "never-configured undo restore")


def _cases() -> dict[str, dict[str, str | None]]:
    cases: dict[str, dict[str, str | None]] = {}
    for app_id, seed in DESKTOP_SEEDS.items():
        cases[app_id] = {"seed": seed, "empty": "", "missing": None}
    cases[CODEX] = {**cases[CODEX], **CODEX_SEEDS}
    return cases


def _record_surface(record: Path, document: Path | None) -> str:
    """The side record, with its machine-specific facts reduced to their meaning.

    ``prior_line`` is 7.111.4's addition and is compared by its own tests; the
    hash is reduced to "does it name the document as it is now", and the undo
    stamp to whether there is one.
    """

    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except OSError:
        return "absent"
    current = ""
    if document is not None and document.exists():
        current = hashlib.sha256(document.read_bytes()).hexdigest()
    for entry in (payload.get("subjects") or {}).values():
        entry["document_path"] = Path(entry.get("document_path", "")).as_posix()
        entry["document_sha256"] = entry.get("document_sha256") == current
        entry["undone_at"] = bool(entry.get("undone_at"))
        for value in entry.get("overwritten") or []:
            value.pop("prior_line", None)
    for entry in (payload.get("mcp_servers") or {}).values():
        entry["document_path"] = Path(entry.get("document_path", "")).as_posix()
        entry["added_at"] = bool(entry.get("added_at"))
    return json.dumps(payload, sort_keys=True)


def _outcome(call: Callable[[], object]) -> str:
    try:
        result = call()
    except DesktopApplyError as exc:
        # JSON-quoted, so a path in the message is normalised like any other.
        return "refused: " + json.dumps(str(exc))
    assert dataclasses.is_dataclass(result) and not isinstance(result, type)
    return json.dumps(dataclasses.asdict(result), sort_keys=True)


def _run(
    app_id: str,
    seed: str | None,
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
    services: Any,
    settings: Any,
    steps: tuple[str, ...],
) -> dict[str, str]:
    home, mcc = work / "home", work / "mcc"
    env = env_for(home)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    for name in ("CODEX_HOME", "OPENCODE_CONFIG_DIR", "CRUSH_GLOBAL_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCC_CONFIG_DIR", str(mcc))
    paths.reset_config_dir_cache()
    spec = desktop_app(app_id)
    make_marker(home, spec)
    document = document_path_for(spec, env)
    sidecar = sidecar_path_for(spec, env)
    if seed is not None and document is not None:
        document.parent.mkdir(parents=True, exist_ok=True)
        document.write_text(seed, encoding="utf-8", newline="")
    block, scalars, sidecar_document = _documents(spec, services, settings)
    record = mcc / "restore-record.json"

    def configure() -> object:
        return desktop_apply.apply(
            spec,
            env=env,
            block=block,
            scalars=scalars,
            sidecar_document=sidecar_document,
        )

    def undo(mode: UndoMode) -> Callable[[], object]:
        return lambda: desktop_apply.undo(spec, env=env, mode=mode)

    def plan() -> object:
        result = desktop_apply.plan(
            spec,
            env=env,
            block=block,
            scalars=scalars,
            sidecar_document=sidecar_document,
        )
        return result

    calls: dict[str, Callable[[], object]] = {
        "plan": plan,
        "configure": configure,
        "configure again": configure,
        "undo keys_only": undo(UndoMode.KEYS_ONLY),
        "undo restore": undo(UndoMode.RESTORE),
        "never-configured undo keys_only": undo(UndoMode.KEYS_ONLY),
        "never-configured undo restore": undo(UndoMode.RESTORE),
    }
    surface: dict[str, str] = {}
    for step in steps:
        surface[step] = _outcome(calls[step])
        files: dict[str, str | None] = {}
        for label, path in (("document", document), ("sidecar", sidecar)):
            if path is not None:
                files[label] = (
                    path.read_text(encoding="utf-8", newline="")
                    if path.exists()
                    else None
                )
        files["record"] = _record_surface(record, document)
        surface[f"{step}:files"] = json.dumps(files, sort_keys=True)
    return {
        key: _norm_escaped(value, work).replace("\\\\", "/")
        for key, value in surface.items()
    }


def _surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apps: tuple[str, ...]):
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "18999")
    settings = _settings(None)
    services: Any = types.SimpleNamespace(requests=FakeRuntime(settings=settings))
    surface: dict[str, str] = {}
    for app_id, seeds in _cases().items():
        if app_id not in apps:
            continue
        for seed_name, seed in seeds.items():
            for kind, steps in (("cycle", CYCLE), ("never", NEVER)):
                work = tmp_path / f"{app_id}-{seed_name}-{kind}"
                work.mkdir()
                for step, value in _run(
                    app_id, seed, work, monkeypatch, services, settings, steps
                ).items():
                    surface[f"{app_id}:{seed_name}:{step}"] = value
    return surface


def _write_or_compare(surface: dict[str, str], *, codex: bool) -> list[str]:
    if WRITE_BASELINE:
        target = Path(WRITE_BASELINE)
        existing = (
            json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
        )
        existing.update(surface)
        target.write_text(
            json.dumps(existing, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        pytest.skip("wrote the baseline")
    baseline = {
        key: value
        for key, value in json.loads(BASELINE.read_text(encoding="utf-8")).items()
        if key.startswith(f"{CODEX}:") == codex
    }
    assert sorted(surface) == sorted(baseline)
    return sorted(key for key in baseline if surface[key] != baseline[key])


def _expected_changes() -> set[str]:
    changed: set[str] = set()
    for name in SHAPE_ONE:
        # The plan's diff, and every file the cycle writes, now keep the
        # user's line. A plan writes nothing, so its files do not move.
        changed.add(f"{CODEX}:{name}:plan")
        for step in CYCLE[1:]:
            changed.add(f"{CODEX}:{name}:{step}:files")
    for name in SHAPE_TWO:
        changed.add(f"{CODEX}:{name}:never-configured undo keys_only")
        changed.add(f"{CODEX}:{name}:never-configured undo keys_only:files")
        changed.add(f"{CODEX}:{name}:never-configured undo restore:files")
    return changed


def test_the_codex_cycle_changes_only_the_two_defect_shapes(tmp_path, monkeypatch):
    surface = _surface(tmp_path, monkeypatch, (CODEX,))
    different = _write_or_compare(surface, codex=True)
    # The measured set against the stated one, both ways: nothing outside the
    # two shapes moved, and every listed key did.
    expected = _expected_changes()
    assert set(different) == expected, sorted(set(different) ^ expected)

    for name in SHAPE_ONE:
        for step in ("undo keys_only", "undo restore"):
            files = json.loads(surface[f"{CODEX}:{name}:{step}:files"])
            assert files["document"] == SHAPE_ONE_AFTER_UNDO[name], (name, step)
        configured = json.loads(surface[f"{CODEX}:{name}:configure:files"])
        assert "mcc/best" in configured["document"], name
    for name in SHAPE_TWO:
        files = json.loads(
            surface[f"{CODEX}:{name}:never-configured undo keys_only:files"]
        )
        assert files["document"] == CODEX_SEEDS[name], name
        outcome = json.loads(surface[f"{CODEX}:{name}:never-configured undo keys_only"])
        assert outcome["changed"] is False, name


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="every other card's document is at Windows' path in the baseline; "
    "the Windows full-suite job runs it",
)
def test_every_other_desktop_document_is_byte_identical(tmp_path, monkeypatch):
    others = tuple(app_id for app_id in DESKTOP_SEEDS if app_id != CODEX)
    surface = _surface(tmp_path, monkeypatch, others)
    different = _write_or_compare(surface, codex=False)
    assert different == [], different
