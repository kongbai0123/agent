from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ROOT / "backend",
    ROOT / "frontend",
    ROOT / "docs",
    ROOT / "scripts",
    ROOT / "config",
    ROOT / "launcher",
    ROOT / "tests",
)
TEXT_SUFFIXES = {
    ".css",
    ".cs",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def test_retired_semantic_verification_feature_is_absent() -> None:
    retired_identifier = "sa" + "fir"
    candidates = [ROOT / "README.md", ROOT / ".gitattributes"]
    for source_root in SOURCE_ROOTS:
        candidates.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and path.suffix.casefold() in TEXT_SUFFIXES
            and "__pycache__" not in path.parts
        )

    matches = []
    for path in candidates:
        content = path.read_text(encoding="utf-8", errors="replace").casefold()
        if retired_identifier in content:
            matches.append(str(path.relative_to(ROOT)))

    assert matches == []
