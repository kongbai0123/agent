import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
STYLE = (FRONTEND / "style.css").read_text(encoding="utf-8")

FONT_STACK = '"Times New Roman", "Microsoft JhengHei UI", "Microsoft JhengHei", serif'


def _first_party_frontend_files():
    for path in FRONTEND.rglob("*"):
        if not path.is_file() or path.suffix not in {".css", ".html", ".js"}:
            continue
        if path.name == "katex.min.css" or "vendor" in path.parts:
            continue
        yield path


def test_all_first_party_interface_font_sizes_are_at_least_twelve_pixels():
    violations = []
    pattern = re.compile(r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)px")
    for path in _first_party_frontend_files():
        source = path.read_text(encoding="utf-8")
        for match in pattern.finditer(source):
            if float(match.group(1)) < 12:
                line = source.count("\n", 0, match.start()) + 1
                violations.append(f"{path.relative_to(ROOT)}:{line}={match.group(1)}px")
    assert violations == []


def test_english_and_chinese_use_the_requested_fallback_stack():
    assert f"--font-sans: {FONT_STACK};" in STYLE
    assert f"--font-mono: {FONT_STACK};" in STYLE
    assert f"font-family: {FONT_STACK};" in (FRONTEND / "loading.html").read_text(encoding="utf-8")
    combined = "\n".join(path.read_text(encoding="utf-8") for path in _first_party_frontend_files())
    for removed_family in (
        "Segoe UI",
        "Noto Sans",
        "IBM Plex Sans",
        "Cascadia Code",
        "Consolas",
        "Fira Code",
        "JetBrains Mono",
    ):
        assert removed_family not in combined


def test_primary_and_secondary_text_keep_a_readable_size_hierarchy():
    required_pairs = (
        (".output-panel-title", "font-size: 13px"),
        (".output-panel-project", "font-size: 12px"),
        (".output-skills-mount .project-skill-name", "font-size: 13px"),
        (".output-skills-mount .project-skill-meta", "font-size: 12px"),
        (".project-name", "font-size: 13px"),
        (".project-count", "font-size: 12px"),
        (".session-item-title", "font-size: 13px"),
        (".session-project-label", "font-size: 12px"),
        (".agent-message-name", "font-size: 13.5px"),
        (".agent-message-time", "font-size: 12px"),
    )
    for selector, declaration in required_pairs:
        rules = re.findall(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", STYLE)
        assert rules, selector
        assert any(declaration in rule for rule in rules)
