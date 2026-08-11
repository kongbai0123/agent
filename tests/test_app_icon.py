import struct
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "frontend" / "app-icon.png"
INDEX = ROOT / "frontend" / "index.html"
LOADING = ROOT / "frontend" / "loading.html"


class AppIconTests(unittest.TestCase):
    def test_icon_is_square_rgba_png(self):
        payload = ICON.read_bytes()
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
        width, height, bit_depth, color_type = struct.unpack(
            ">IIBB", payload[16:26]
        )
        self.assertEqual((width, height), (1024, 1024))
        self.assertEqual(bit_depth, 8)
        self.assertEqual(color_type, 6, "app icon must retain an RGBA alpha channel")

    def test_main_and_loading_pages_share_the_brand_asset(self):
        for page in (INDEX, LOADING):
            markup = page.read_text(encoding="utf-8")
            self.assertIn(
                '<link rel="icon" type="image/png" href="app-icon.png?v=1">',
                markup,
            )
            self.assertIn('src="app-icon.png?v=1"', markup)

    def test_main_brand_no_longer_uses_the_cpu_placeholder(self):
        markup = INDEX.read_text(encoding="utf-8")
        self.assertNotIn('data-lucide="cpu" class="logo-icon"', markup)


if __name__ == "__main__":
    unittest.main()
