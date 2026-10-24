"""Configure and Undo on Codex leave the user's own ``model`` line alone (7.111.4).

Two defects, both since 7.99.0 and found by 7.100.0's rig:

* Configure then Undo, in either mode, dropped a comment written on the same
  line as the user's ``model`` -- Configure rewrote the whole line and the
  restore record kept only the value.
* Undo "Remove MCC's keys" on a Codex that MCC never configured deleted the
  user's ``model`` line.

Every Codex document here lives in a ``tmp_path`` home; nothing reads or
writes a real ``~/.codex``.
"""

import json

import pytest

from my_claude_code.config import desktop_apply
from my_claude_code.config.desktop_apps import desktop_app
from my_claude_code.config.restore_record import UndoMode, read_entry
from tests.config.test_desktop_apply import BLOCK, document_path, env_for, make_marker

SCALARS = {"model_provider": "mcc", "model": "mcc/best"}
#: The repro's seed (``D:/mcc-scratch-mcp2/scripts/base_codex_comment.py``).
SEED = '# mine\nmodel = "gpt-5.6"   # my default\n\n[profiles.fast]\nmodel = "x"\n'


def _codex(tmp_path, text: str | None):
    spec = desktop_app("codex_desktop")
    make_marker(tmp_path, spec)
    env = env_for(tmp_path)
    path = document_path(spec, env)
    if text is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
    return spec, env, path, tmp_path / "record.json"


def _configure(spec, env, record) -> None:
    desktop_apply.apply(spec, env=env, block=BLOCK, scalars=SCALARS, record_path=record)


def _read(path) -> str:
    return path.read_text(encoding="utf-8", newline="")


@pytest.mark.parametrize("mode", [UndoMode.KEYS_ONLY, UndoMode.RESTORE])
@pytest.mark.parametrize(
    "seed, configured_line",
    [
        (SEED, 'model = "mcc/best"   # my default'),
        # Tabs.
        (
            '\tmodel_provider = "openai"\t# mine\n\tmodel = "gpt-5.6"\t# tabbed\n'
            '\n[profiles.fast]\nmodel = "x"\n',
            '\tmodel = "mcc/best"\t# tabbed',
        ),
        # Four spaces and wide spacing around ``=``.
        (
            '    model  =  "gpt-5.6"    # spaced\n[profiles.fast]\nmodel = "x"\n',
            '    model  =  "mcc/best"    # spaced',
        ),
        # A quoted key and a literal string.
        (
            '"model" = \'gpt-5.6\' # literal\n\n[profiles.fast]\nmodel = "x"\n',
            "\"model\" = 'mcc/best' # literal",
        ),
        # An escape no re-rendering of the value would spell the same way: only
        # the recorded line gives it back.
        (
            'model = "gpt\\u002d5.6" # escaped\n\n[profiles.fast]\nmodel = "x"\n',
            'model = "mcc/best" # escaped',
        ),
        # No trailing newline, and the model line is the last line.
        (
            'model_provider = "openai" # mine\nmodel = "gpt-5.6" # last',
            'model = "mcc/best" # last',
        ),
    ],
    ids=[
        "comment",
        "tabs",
        "four-space",
        "quoted-literal",
        "escaped",
        "no-trailing-newline",
    ],
)
def test_configure_then_undo_gives_the_users_model_line_back(
    tmp_path, seed, configured_line, mode
):
    spec, env, path, record = _codex(tmp_path, seed)

    _configure(spec, env, record)
    # Configure changed the value and kept everything else on the line.
    assert configured_line in _read(path).splitlines()

    result = desktop_apply.undo(spec, env=env, mode=mode, record_path=record)

    # A file without a trailing newline gains one through the cycle: MCC's
    # table is appended after a newline and removed with it. That is the table
    # path, older than and untouched by 7.111.4; every line comes back as is.
    expected = seed if seed.endswith("\n") else seed + "\n"
    assert _read(path) == expected
    assert "model" in result.restored_keys


def test_the_record_keeps_the_users_line_and_only_for_a_key_that_was_there(tmp_path):
    spec, env, _path, record = _codex(tmp_path, SEED)

    _configure(spec, env, record)

    entry = read_entry(spec.id, path=record)
    assert entry is not None
    lines = {value.key_path: value.prior_line for value in entry.overwritten}
    assert lines == {
        ("model_provider",): "",
        ("model",): 'model = "gpt-5.6"   # my default',
    }
    payload = json.loads(record.read_text(encoding="utf-8"))
    written = payload["subjects"][spec.id]["overwritten"]
    # Written only where there is a line: an absent key's entry is unchanged.
    assert [sorted(value) for value in written] == [
        ["key_path", "prior_present", "prior_value"],
        ["key_path", "prior_line", "prior_present", "prior_value"],
    ]


def test_a_record_written_before_the_line_was_kept_still_undoes(tmp_path):
    """IV.13: an old record (no ``prior_line``) still restores, by value."""

    spec, env, path, record = _codex(tmp_path, SEED)
    _configure(spec, env, record)
    payload = json.loads(record.read_text(encoding="utf-8"))
    for value in payload["subjects"][spec.id]["overwritten"]:
        value.pop("prior_line", None)
    record.write_text(json.dumps(payload), encoding="utf-8")

    result = desktop_apply.undo(
        spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record
    )

    # The value edit alone already keeps the comment Configure kept.
    assert _read(path) == SEED
    assert result.restored_keys == ("model",)


@pytest.mark.parametrize(
    "seed",
    [
        SEED,
        'model_provider = "openai" # mine\nmodel = "gpt-5.6"\n',
        'model = "gpt-5.6"\r\n\r\n[profiles.fast]\r\nmodel = "x"\r\n',
    ],
    ids=["comment", "both-keys", "crlf"],
)
def test_keys_only_undo_on_a_never_configured_codex_changes_nothing(tmp_path, seed):
    spec, env, path, record = _codex(tmp_path, seed)

    result = desktop_apply.undo(
        spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record
    )

    assert _read(path) == seed
    assert result.changed is False
    assert result.restored_keys == ()
    assert spec.document is not None
    assert not path.with_name(path.name + spec.document.backup_suffix).exists()


def test_restore_undo_on_a_never_configured_codex_still_refuses_untouched(tmp_path):
    spec, env, path, record = _codex(tmp_path, SEED)

    with pytest.raises(desktop_apply.DesktopApplyError, match="no record"):
        desktop_apply.undo(spec, env=env, mode=UndoMode.RESTORE, record_path=record)

    assert _read(path) == SEED


def test_a_second_keys_only_undo_leaves_a_line_the_user_wrote_since(tmp_path):
    # After an Undo the record is kept and stamped; MCC's table is gone. A
    # line the user writes afterwards is theirs, and a second Undo must not
    # delete it or put the old value over it.
    spec, env, path, record = _codex(tmp_path, SEED)
    _configure(spec, env, record)
    desktop_apply.undo(spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record)
    edited = 'model_provider = "openai" # new\n' + SEED.replace("gpt-5.6", "gpt-7")
    path.write_text(edited, encoding="utf-8", newline="")

    result = desktop_apply.undo(
        spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record
    )

    assert _read(path) == edited
    assert result.changed is False


def test_keys_only_undo_still_takes_mccs_keys_out_when_its_table_is_there(tmp_path):
    # No record (lost), but MCC's provider table is in the file: MCC did
    # configure it, and Undo removes what it can name, as it always has.
    spec, env, path, record = _codex(tmp_path, SEED)
    _configure(spec, env, record)
    record.unlink()

    result = desktop_apply.undo(
        spec, env=env, mode=UndoMode.KEYS_ONLY, record_path=record
    )

    text = _read(path)
    assert result.changed is True
    assert "[model_providers.mcc]" not in text
    assert "model_provider" not in text
