"""Render the GitHub social preview card at exactly 1280x640.

    uv run --offline python scripts/render_social_preview.py

Writes ``assets/social-preview.png``. Upload it by hand at Settings ->
General -> Social preview; GitHub exposes no API for that field, which is why
this is a script and a committed PNG rather than a workflow.

WHY A DRAWN CARD AND NOT A SCREENSHOT

    The card is rendered at ~400px wide in a Discord embed and ~500px in a
    Twitter card. A dashboard screenshot at that size is a grey smear: the
    thing it has to do -- say the project's name and what it is, legibly, to
    someone scrolling -- is the one thing a screenshot of a dense table cannot.

    There is a second reason, and it is the stronger one. A screenshot of a
    live install carries what the install knows: masked credential labels
    (``sk-1...rnp1`` still reveals eight real characters of a real key), the
    operator's home directory and therefore their username, model names, spend.
    Part IV section 4 of the working notes already requires doc screenshots to
    be metadata-only, and 6.39.1 had to re-shoot a shipped PNG for exactly this
    -- a masked real key label. A social preview is the most widely reproduced
    image a repository has; it is the worst place to learn that lesson again.

COLOURS AND THE MARK

    Colours are the dashboard's own Velvet tokens read from ``admin.css``:
    background ``#0a1220``, text ``#e8eaf2``, accent ``#e6435e``. The mark is
    the shipped ``app-icon.png``, composited as-is at its own aspect ratio --
    BRAND.md forbids recolouring, stretching or cropping it.

    Fonts are the system UI faces, resolved at render time. BRAND.md asks for
    Fira, and falls back to the system stack when Fira is absent, which it is
    on this machine; a web-font fetch inside a build script would be a
    dependency on a network for a file that changes once a year.
"""

import pathlib

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 640
BG = (10, 18, 32)  # --bg, Velvet
TEXT = (232, 234, 242)  # --text
STRONG = (255, 255, 255)  # --text-strong
ACCENT = (230, 67, 94)  # --accent, Velvet
MUTED = (150, 158, 178)
RULE = (30, 42, 62)

REPO = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO / "assets" / "social-preview.png"

SEGOE = "C:/Windows/Fonts/segoeui.ttf"
SEGOE_B = "C:/Windows/Fonts/segoeuib.ttf"
MONO = "C:/Windows/Fonts/consola.ttf"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def main() -> None:
    card = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(card)

    # A soft accent wash in the top-left, so the card is not a flat rectangle.
    # Drawn as a mask and blurred: stacked ellipses leave a visible hard arc,
    # which reads as a rendering fault rather than as a design.
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse((-380, -430, 620, 420), fill=88)
    mask = mask.filter(ImageFilter.GaussianBlur(160))
    card = Image.composite(Image.new("RGB", (W, H), ACCENT), card, mask)
    draw = ImageDraw.Draw(card)

    # The shipped mark, untouched.
    icon_path = REPO / "src/my_claude_code/assets/app-icon.png"
    icon = Image.open(icon_path).convert("RGBA")
    side = 104
    # `Image.Resampling.LANCZOS`, not the `Image.LANCZOS` alias: the alias is a
    # deprecated re-export that still works at runtime but is absent from
    # Pillow's type stubs, so `ty` rejects it.
    icon = icon.resize((side, side), Image.Resampling.LANCZOS)
    card.paste(icon, (88, 92), icon)

    x = 88 + side + 30
    draw.text((x, 100), "My Claude Code", font=font(SEGOE_B, 62), fill=STRONG)
    draw.text((x, 172), "MCC", font=font(MONO, 26), fill=ACCENT)

    # The one line that has to survive being shrunk to a Discord thumbnail.
    draw.text(
        (88, 268),
        "Local LLM proxy & model router",
        font=font(SEGOE_B, 54),
        fill=TEXT,
    )
    draw.text(
        (88, 330),
        "for AI coding agents",
        font=font(SEGOE_B, 54),
        fill=TEXT,
    )

    draw.line((88, 424, W - 88, 424), fill=RULE, width=2)

    # Clients, named rather than logo'd: names survive downscaling, logos do not.
    draw.text(
        (88, 452),
        "Claude Code  ·  Codex  ·  OpenCode  ·  Gemini CLI  ·  Cline  ·  Aider  ·  Crush",
        font=font(SEGOE, 27),
        fill=MUTED,
    )

    stats = [
        ("57", "providers"),
        ("4", "API protocols"),
        ("16", "agents"),
    ]
    sx = 88
    for value, label in stats:
        vf, lf = font(SEGOE_B, 40), font(SEGOE, 24)
        draw.text((sx, 520), value, font=vf, fill=ACCENT)
        vw = draw.textlength(value, font=vf)
        draw.text((sx + vw + 10, 534), label, font=lf, fill=MUTED)
        sx += int(vw + draw.textlength(label, font=lf)) + 62

    draw.text(
        (W - 88, 534),
        "github.com/FiredMosquito831/my-claude-code",
        font=font(MONO, 22),
        fill=MUTED,
        anchor="rs",
    )

    card.save(OUT, "PNG", optimize=True)
    print(f"{OUT}  {card.size[0]}x{card.size[1]}  {OUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
