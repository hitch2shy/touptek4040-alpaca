from __future__ import annotations

from pathlib import Path

import pytest

from test.scripts.normalize_text_artifacts import normalize_text_artifact


def test_generated_evidence_is_committed_with_lf_line_endings() -> None:
    attributes = (Path(__file__).parents[2] / ".gitattributes").read_text(
        encoding="utf-8"
    )

    assert "test/results/hardware/** text eol=lf" in attributes.splitlines()
    assert "test/results/conformu/** text eol=lf" in attributes.splitlines()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_normalize_text_artifact_removes_trailing_space_and_extra_eof_lines(
    tmp_path: Path,
    encoding: str,
) -> None:
    artifact = tmp_path / "evidence.log"
    artifact.write_bytes(
        "first  \r\nsecond\t\r\n\r\n".encode(encoding)
    )

    normalize_text_artifact(artifact)

    assert artifact.read_bytes() == b"first\nsecond\n"


def test_normalize_text_artifact_applies_literal_path_replacement(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "pytest.txt"
    artifact.write_text("G:\\work\\repo\\test  \n", encoding="utf-8")

    normalize_text_artifact(
        artifact,
        replacements=(("G:\\work\\repo", "<repo>"),),
    )

    assert artifact.read_bytes() == b"<repo>\\test\n"
