"""The Mistral image surface is declared on ``mistral`` and on nothing else.

Spec §9.5, and it is the WHOLE of decision 2's enforcement. Nothing else
refuses a ``mistral_vibe`` ref on the Image rail: a provider that declares no
surface for an operation is skipped uncharged by the capability gate
(``providers/media/leaf.py::surfaces_serve``), and that absence IS the
enforcement. This test exists so a future contributor cannot add the surface
"for convenience" without failing CI.

Why the absence matters (spec E10/E19): both Mistral credentials authenticate
to the same host and differ in what they draw on, and the image charge's pool
is NOT established as the Vibe subscription's coding budget. Declaring the
surface on ``mistral_vibe`` would advertise a route that may be refused, or
billed to an allowance nobody agreed to spend.
"""

from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_SHAPE_MISTRAL_IMAGE_TOOL,
    mistral_image_tool_surface,
    surface_for,
)
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG


def test_exactly_one_descriptor_declares_the_mistral_image_shape() -> None:
    declaring = sorted(
        provider_id
        for provider_id, descriptor in PROVIDER_CATALOG.items()
        if any(
            surface.shape == MEDIA_SHAPE_MISTRAL_IMAGE_TOOL
            for surface in descriptor.media_surfaces
        )
    )
    assert declaring == ["mistral"]


def test_mistral_vibe_declares_no_image_surface_at_all() -> None:
    """The refusal is by absence: nothing needs to be written to make it."""

    descriptor = PROVIDER_CATALOG["mistral_vibe"]
    assert descriptor.media_surfaces == ()
    assert (
        surface_for(descriptor.media_surfaces, MEDIA_OPERATION_IMAGE_GENERATE) is None
    )


def test_mistral_serves_image_generation_through_the_tool_shape() -> None:
    surface = surface_for(
        PROVIDER_CATALOG["mistral"].media_surfaces, MEDIA_OPERATION_IMAGE_GENERATE
    )
    assert surface is not None
    assert surface.shape == MEDIA_SHAPE_MISTRAL_IMAGE_TOOL
    assert surface == mistral_image_tool_surface()


def test_mistrals_transcription_surface_is_untouched() -> None:
    """8.14.0 ADDS a surface; the one the provider PR shipped is unchanged."""

    shapes = [surface.shape for surface in PROVIDER_CATALOG["mistral"].media_surfaces]
    assert shapes == ["openai_transcriptions", MEDIA_SHAPE_MISTRAL_IMAGE_TOOL]


def test_no_other_mistral_rail_is_declared() -> None:
    """Q6: no edit surface, no speech, no video -- only the two above."""

    operations = sorted(
        surface.operation for surface in PROVIDER_CATALOG["mistral"].media_surfaces
    )
    assert operations == ["image_generate", "transcribe"]
