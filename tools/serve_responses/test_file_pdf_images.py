"""Decode actual isolated PDF outputs; requires the matching C++ fixture run."""
import os
from pathlib import Path
import unittest

from PIL import Image


class PdfImages(unittest.TestCase):
    def test_actual_pages_have_expected_pixels_and_rotation(self):
        root = Path(os.environ["Q4T_TEST_OUTPUT_DIR"])
        for name, size, color in (
            ("pdf-low-1.png", (512, 256), (255, 0, 0)),
            ("pdf-low-2.png", (512, 256), (0, 0, 255)),
            ("pdf-high-1.png", (1024, 2048), (255, 0, 0)),
        ):
            with self.subTest(page=name), Image.open(root / name) as encoded:
                self.assertEqual(encoded.format, "PNG")
                self.assertEqual(encoded.size, size)
                image = encoded.convert("RGB")
                self.assertEqual(image.getpixel((10, 10)), color)
                counts = image.getcolors(size[0] * size[1])
                actual = dict((value, count) for count, value in counts)
                self.assertGreater(actual.get(color, 0), size[0] * size[1] * .9)
                # Text is actually rasterized; a uniform metadata-only page
                # or substituted solid image cannot pass this check.
                self.assertGreater(actual.get((0, 0, 0), 0), 100)


if __name__ == "__main__":
    unittest.main()
