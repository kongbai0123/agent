from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


class FrontendMathRenderingTests(unittest.TestCase):
    def test_math_is_protected_before_markdown_and_restored_after_sanitizing(self):
        renderer = JS.split("function renderMarkdownSafe", 1)[1].split("function parseMarkdown", 1)[0]
        self.assertIn("protectMathForMarkdown(text)", renderer)
        self.assertIn("marked.parse(protectedMath.text", renderer)
        self.assertIn("DOMPurify.sanitize", renderer)
        self.assertIn("restoreMathAfterMarkdown(sanitized", renderer)

    def test_protector_supports_inline_display_and_bracket_delimiters(self):
        protector = JS.split("function protectMathForMarkdown", 1)[1].split("function restoreMathAfterMarkdown", 1)[0]
        for delimiter in ("'$$'", "'\\\\['", "'\\\\('", "'$'"):
            self.assertIn(delimiter, protector)
        self.assertIn("fencedCode", protector)
        self.assertIn("inlineCode", protector)
        self.assertIn("escapedAt", protector)

    def test_local_katex_and_auto_render_are_loaded(self):
        self.assertIn('href="katex.min.css"', HTML)
        self.assertIn('src="katex.min.js"', HTML)
        self.assertIn('src="auto-render.min.js"', HTML)
        self.assertRegex(HTML, r'href="style\.css\?v=[^"]+"')
        self.assertRegex(HTML, r'src="app\.js\?v=[^"]+"')


if __name__ == "__main__":
    unittest.main()
