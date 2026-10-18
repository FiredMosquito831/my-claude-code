"""Where ``mcc-mcp`` saves a result, and that it never overwrites one (7.99.0)."""

from pathlib import Path

from my_claude_code.mcp_media.outputs import (
    SessionLog,
    extension_for,
    named,
    resolve_target,
    save_new_file,
    sniff_image_type,
)


def test_a_file_is_never_overwritten(tmp_path: Path) -> None:
    """Decision 5: a name already taken gets -1, -2 ... and the old file stays."""

    existing = tmp_path / "kite.png"
    existing.write_bytes(b"the user's own picture")

    first = save_new_file(b"one", tmp_path, "kite.png")
    second = save_new_file(b"two", tmp_path, "kite.png")

    assert existing.read_bytes() == b"the user's own picture"
    assert first == (tmp_path / "kite-1.png").resolve()
    assert second == (tmp_path / "kite-2.png").resolve()
    assert first.read_bytes() == b"one" and second.read_bytes() == b"two"


def test_save_to_names_a_folder_or_a_file(tmp_path: Path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    default = tmp_path / "default"

    assert resolve_target(None, default_dir=default, cwd=cwd) == (default, None)
    assert resolve_target("  ", default_dir=default, cwd=cwd) == (default, None)
    assert resolve_target("art", default_dir=default, cwd=cwd) == (cwd / "art", None)
    assert resolve_target("art/", default_dir=default, cwd=cwd) == (cwd / "art", None)
    assert resolve_target("art/hero.png", default_dir=default, cwd=cwd) == (
        cwd / "art",
        "hero.png",
    )
    absolute = tmp_path / "elsewhere" / "x.webp"
    assert resolve_target(str(absolute), default_dir=default, cwd=cwd) == (
        absolute.parent,
        "x.webp",
    )


def test_names_and_types() -> None:
    assert named("hero.png", ".png", "image", 0) == "hero.png"
    assert named("hero.png", ".png", "image", 1) == "hero-1.png"
    assert named(None, ".png", "image", 0).startswith("mcc-image-")
    assert named(None, ".png", "image", 1).endswith("-2.png")
    assert sniff_image_type(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_type(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert extension_for("audio/mpeg") == ".mp3"
    assert extension_for("video/mp4; codecs=avc1") == ".mp4"
    assert extension_for("") == ".bin"


def test_the_session_log_keeps_late_results() -> None:
    log = SessionLog()
    log.add("generate_image", "1 picture(s)", paths=["a.png"])
    log.add("speak", "speech", after_app_stopped_waiting=True)
    first, second = log.snapshot()
    assert first.paths == ("a.png",) and not first.after_app_stopped_waiting
    assert second.after_app_stopped_waiting is True
