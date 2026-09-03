"""The LICENSE file is a legal document, so it gets a mechanical guard.

Four things about it can break silently, and none of them fails anything else
in the suite:

* **the license text itself** -- the GNU AGPL v3 is quoted verbatim from
  <https://www.gnu.org/licenses/agpl-3.0.txt>. A well-meaning reflow, a dropped
  section, or the old PolyForm text pasted back in changes what users are
  allowed to do. Section 13, the clause that makes this AGPL rather than GPL,
  is pinned by name.
* **the section 7(b) attribution term** -- it is the whole mechanism by which
  the author gets credited. Section 7 permits exactly this kind of term; a
  broader one (a duty to link back whenever the project is mentioned) is *not*
  permitted there, and belongs in the preface as a request. This guard checks
  the term is present and stays inside 7(b).
* **the inherited MIT notice** -- roughly half of the non-trivial source lines
  in this project still come verbatim from the upstream MIT-licensed
  ``Alishahryar1/free-claude-code``. Dropping Ali Khokhar's copyright line is a
  license violation, not a formatting choice.
* **the packaging** -- a wheel that does not carry ``LICENSE`` ships the code
  without its terms, and one that does not carry ``COMMERCIAL-LICENSE.md``
  hides the only route to using it outside the AGPL. ``license-files`` in
  ``pyproject.toml`` is what puts both there, and hatchling drops them without
  complaint if that line goes.

The word "noncommercial" is checked to be gone from the product surface too:
the project moved off PolyForm Noncommercial in 6.39.2, and a stale badge or
paragraph telling a company it may not use this is a real cost to them.
"""

import os
import re
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LICENSE_PATH = REPO_ROOT / "LICENSE"
COMMERCIAL_PATH = REPO_ROOT / "COMMERCIAL-LICENSE.md"


def _read(path: Path) -> str:
    # The worktree checks out CRLF; read with universal newlines so the
    # literals below never have to think about "\r".
    with open(path, encoding="utf-8", newline=None) as handle:
        return handle.read()


def _flat(text: str) -> str:
    """Collapse whitespace, so an assertion about a *sentence* is not secretly
    an assertion about where the FSF's fixed-width text happens to wrap."""

    return " ".join(text.split())


@pytest.fixture(scope="module")
def license_text() -> str:
    return _read(LICENSE_PATH)


# --------------------------------------------------------------------------
# The Required Notice -- what section 7(b) below obliges everyone to keep.
# --------------------------------------------------------------------------


def test_the_required_notice_opens_the_file(license_text: str) -> None:
    assert license_text.startswith(
        "Required Notice: Copyright (c) 2026 FiredMosquito831\n"
        "                   https://github.com/FiredMosquito831/my-claude-code\n"
    ), (
        "the Required Notice block must be the first thing in LICENSE: the "
        "section 7(b) term below is written in terms of 'the plain-text lines "
        "beginning with `Required Notice:` at the top of this file', and that "
        "is how the author gets credited."
    )


# --------------------------------------------------------------------------
# The GNU AGPL v3, verbatim.
# --------------------------------------------------------------------------


def test_the_agpl_title_lines_are_present(license_text: str) -> None:
    assert (
        "                    GNU AFFERO GENERAL PUBLIC LICENSE\n"
        "                       Version 3, 19 November 2007\n" in license_text
    ), "the AGPL title block must be reproduced exactly as the FSF publishes it"
    assert (
        " Copyright (C) 2007 Free Software Foundation, Inc. <https://fsf.org/>"
        in license_text
    ), "the FSF's own copyright line on the license document must not be edited"


def test_section_13_remote_network_interaction_survives(license_text: str) -> None:
    """Section 13 is the entire reason this is the AGPL and not the GPL: it is
    what obliges a hosted, modified version to offer its users source."""

    assert (
        "  13. Remote Network Interaction; Use with the GNU General Public"
        " License.\n" in license_text
    )
    assert (
        "an opportunity to receive the Corresponding Source of your version by"
        " providing access to the Corresponding Source from a network server at"
        " no charge" in _flat(license_text)
    ), (
        "section 13's operative sentence is missing or altered; without it the "
        "network-service obligation the README promises does not exist."
    )


@pytest.mark.parametrize(
    "heading",
    [
        "  0. Definitions.",
        "  5. Conveying Modified Source Versions.",
        "  6. Conveying Non-Source Forms.",
        "  7. Additional Terms.",
        "  8. Termination.",
        "  13. Remote Network Interaction; Use with the GNU General Public License.",
        "  15. Disclaimer of Warranty.",
        "  16. Limitation of Liability.",
    ],
)
def test_every_pinned_agpl_section_survives(license_text: str, heading: str) -> None:
    assert f"\n{heading}\n" in license_text, (
        f"{heading!r} is missing from LICENSE. The AGPL is quoted verbatim; a "
        "missing section is a changed license."
    )


def test_section_7b_is_quoted_before_the_additional_term(license_text: str) -> None:
    """The additional term stands or falls on being one section 7 allows."""

    assert (
        "b) Requiring preservation of specified reasonable legal notices or"
        " author attributions in that material or in the Appropriate Legal"
        " Notices displayed by works containing it; or" in _flat(license_text)
    ), "AGPL section 7(b) itself must be present and unedited in the quoted text"


def test_the_previous_polyform_license_is_gone(license_text: str) -> None:
    """6.39.1 shipped PolyForm Noncommercial. Two license texts in one file
    would be contradictory, and a revert would otherwise be invisible."""

    assert "PolyForm" not in license_text
    assert "## Noncommercial Purposes" not in license_text


# --------------------------------------------------------------------------
# The section 7(b) additional term.
# --------------------------------------------------------------------------


def test_the_attribution_term_is_stated_under_section_7b(license_text: str) -> None:
    term = license_text.split("# Additional term under AGPL section 7(b)", 1)
    assert len(term) == 2, (
        "LICENSE must carry the attribution requirement as a named additional "
        "term under AGPL section 7(b). Stated anywhere else it is a further "
        "restriction, which section 7 lets recipients strip out."
    )
    body = term[1].split("# The MIT License (MIT)", 1)[0]
    assert "You must preserve the plain-text lines beginning with" in body
    assert "`Required Notice:`" in body
    assert "Appropriate Legal Notices" in body, (
        "the term must use the AGPL's own vocabulary; 'Appropriate Legal "
        "Notices' is what section 7(b) attaches the obligation to."
    )
    assert "That is the only additional term." in body, (
        "the file must say this is the only additional term, so no reader has "
        "to guess whether something else in it is meant to bind them."
    )


def test_the_link_back_request_is_not_written_as_a_term(license_text: str) -> None:
    """Section 7(b) covers preserving notices, not a duty to link back whenever
    the project is named. That ask lives in the preface, as a request."""

    preface, _, rest = license_text.partition("GNU AFFERO GENERAL PUBLIC LICENSE")
    assert "as a request rather than a condition" in _flat(preface), (
        "the preface must mark the link-back ask as a request; as a term it "
        "would be a further restriction the AGPL does not permit."
    )
    term_body = rest.split("# Additional term under AGPL section 7(b)", 1)[1]
    term_body = term_body.split("# The MIT License (MIT)", 1)[0]
    for forbidden in ("link", "mention", "write about"):
        assert forbidden not in term_body.lower(), (
            f"the additional term mentions {forbidden!r}; keep it to preserving "
            "the Required Notice, which is all section 7(b) allows."
        )


# --------------------------------------------------------------------------
# The inherited MIT code.
# --------------------------------------------------------------------------


def test_the_mit_notice_for_inherited_code_is_intact(license_text: str) -> None:
    assert "# The MIT License (MIT)" in license_text
    assert "Copyright (c) 2026 Ali Khokhar" in license_text, (
        "upstream's copyright line must appear exactly as their LICENSE "
        "states it; substantial portions of Free Claude Code are still in "
        "this tree and MIT requires the notice to travel with them."
    )
    assert "Free Claude Code by Ali Khokhar" in license_text, (
        "the MIT block must name the upstream project it covers, or a reader "
        "cannot tell which parts of this repository it applies to."
    )
    for clause in (
        "Permission is hereby granted, free of charge",
        "The above copyright notice and this permission notice shall be included in all",
        'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
    ):
        assert clause in license_text, clause


# --------------------------------------------------------------------------
# The plain-language preface -- the part users actually read.
# --------------------------------------------------------------------------


def test_the_preface_states_both_halves_of_the_dual_license(
    license_text: str,
) -> None:
    preface = license_text.split("GNU AFFERO GENERAL PUBLIC LICENSE", 1)[0]
    for phrase in (
        "dual-licensed",
        "version 3 or (at your option) any later version",
        "commercial license",
        "COMMERCIAL-LICENSE.md",
        "https://github.com/FiredMosquito831",
    ):
        assert phrase in _flat(preface), (
            f"the preface no longer mentions {phrase!r}; it is the only place "
            "a reader learns that both options exist and how to ask for the "
            "second one."
        )
    assert "@" not in preface, (
        "the preface deliberately routes commercial enquiries through the "
        "GitHub profile and repository issues; no email address belongs here."
    )
    assert "source-available" not in preface, (
        "the AGPL is OSI-approved open source; calling the project "
        "source-available understates what users are being given."
    )


def test_the_commercial_license_document_exists_and_says_what_it_is() -> None:
    text = _read(COMMERCIAL_PATH)
    assert "the only way to use this software outside the terms" in text, (
        "COMMERCIAL-LICENSE.md must state plainly that the commercial license "
        "is the only route outside the AGPL; without that sentence readers "
        "assume an informal exception exists."
    )
    assert "negotiated case by case" in text
    assert "https://github.com/FiredMosquito831" in text
    assert "@" not in text.replace("employer's", ""), (
        "no email address belongs in COMMERCIAL-LICENSE.md; contact runs "
        "through the GitHub profile and repository issues."
    )


# --------------------------------------------------------------------------
# The product surface must not still say "noncommercial".
# --------------------------------------------------------------------------


STALE = re.compile(r"non-?commercial|polyform", re.IGNORECASE)


def test_no_noncommercial_claims_remain_on_the_product_surface() -> None:
    """A company reading "noncommercial" decides it may not run this. It may."""

    targets = [REPO_ROOT / "README.md", COMMERCIAL_PATH]
    targets.extend(
        path
        for path in (REPO_ROOT / "src").rglob("*")
        if path.is_file() and path.suffix in {".py", ".md", ".html", ".js", ".css"}
    )
    offenders = [
        str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        for path in targets
        if STALE.search(_read(path))
    ]
    assert not offenders, (
        f"these files still carry the old licensing language: {offenders}. "
        "Since 6.39.2 the project is dual-licensed AGPL-3.0-or-later or "
        "commercial, and permits commercial use at no cost."
    )


# --------------------------------------------------------------------------
# Packaging: the terms have to ship with the code.
# --------------------------------------------------------------------------


def _pyproject() -> dict:
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)


def test_pyproject_declares_the_license_expression_and_files() -> None:
    project = _pyproject()["project"]
    assert project.get("license") == "AGPL-3.0-or-later AND MIT", (
        "[project] license must be the PEP 639 expression "
        "'AGPL-3.0-or-later AND MIT'. The MIT half is not decorative: "
        "substantial portions of the upstream MIT project are still in this "
        "tree. The commercial option is not an SPDX identifier and is "
        "documented in COMMERCIAL-LICENSE.md instead."
    )
    assert project.get("license-files") == ["LICENSE", "COMMERCIAL-LICENSE.md"], (
        "license-files must name both documents so hatchling copies them into "
        "every wheel's .dist-info. The expression alone ships no terms, and "
        "an installed user with no COMMERCIAL-LICENSE.md has no way to find "
        "the route out of the AGPL."
    )
    classifiers = project.get("classifiers", [])
    assert not any("License ::" in classifier for classifier in classifiers), (
        "PEP 639 forbids pairing a License-Expression with trove license "
        "classifiers, and PyPI rejects distributions that carry both."
    )


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> zipfile.ZipFile:
    """Build a real wheel. Opt-in, for the reason spelled out in
    ``tests/api/test_docs_bundle_wheel.py``: pytest already runs under ``uv``
    and a nested ``uv build`` contending for the same lock has hung CI for the
    full job limit."""

    if os.environ.get("MCC_WHEEL_TESTS") != "1":
        pytest.skip("set MCC_WHEEL_TESTS=1 to build a real wheel (nested uv)")
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    if not (REPO_ROOT / "pyproject.toml").is_file():
        pytest.skip("not running from a source checkout")

    out_dir = tmp_path_factory.mktemp("license-wheel")
    result = subprocess.run(
        [uv, "build", "--wheel", "--offline", "--out-dir", str(out_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=900,
    )
    if result.returncode != 0:
        pytest.fail(f"uv build --wheel failed:\n{result.stderr[-3000:]}")

    wheels = sorted(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return zipfile.ZipFile(wheels[0])


def _dist_info_member(wheel: zipfile.ZipFile, name: str) -> str:
    matches = [
        entry
        for entry in wheel.namelist()
        if re.fullmatch(rf"[^/]+\.dist-info/(licenses/)?{re.escape(name)}", entry)
    ]
    assert matches, (
        f"the built wheel has no {name} under .dist-info. Wheel contents: "
        f"{sorted(wheel.namelist())[:20]}"
    )
    return matches[0]


def test_the_wheel_ships_both_license_documents(built_wheel) -> None:
    license_entry = _dist_info_member(built_wheel, "LICENSE")
    shipped = built_wheel.read(license_entry).decode("utf-8")
    assert shipped.lstrip().startswith("Required Notice:")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in shipped
    assert "13. Remote Network Interaction" in shipped
    assert "Copyright (c) 2026 Ali Khokhar" in shipped

    commercial_entry = _dist_info_member(built_wheel, "COMMERCIAL-LICENSE.md")
    assert built_wheel.getinfo(commercial_entry).file_size > 0

    record_entry = _dist_info_member(built_wheel, "RECORD")
    record = built_wheel.read(record_entry).decode("utf-8")
    for entry in (license_entry, commercial_entry):
        assert entry in record, (
            f"{entry} is in the archive but not in RECORD; installers that "
            "read RECORD will not unpack it."
        )


def test_the_wheel_metadata_declares_the_license(built_wheel) -> None:
    metadata = built_wheel.read(_dist_info_member(built_wheel, "METADATA")).decode(
        "utf-8"
    )
    assert re.search(
        r"^License-Expression: AGPL-3\.0-or-later AND MIT$", metadata, re.MULTILINE
    ), (
        "METADATA must declare the PEP 639 expression naming both halves of "
        f"the terms this package ships under.\n{metadata[:800]}"
    )
    for document in ("LICENSE", "COMMERCIAL-LICENSE.md"):
        assert re.search(
            rf"^License-File: {re.escape(document)}$", metadata, re.MULTILINE
        ), (
            f"METADATA must carry a License-File header naming {document}; "
            "without it tooling that surfaces a package's terms shows "
            f"nothing.\n{metadata[:800]}"
        )
    assert not re.search(r"^Classifier: License ::", metadata, re.MULTILINE)
