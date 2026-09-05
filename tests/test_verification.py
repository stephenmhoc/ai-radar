from __future__ import annotations

import pathlib
import tempfile
import unittest

import radar
from scripts.verify import verify_generated
from tests.test_radar import make_settings, published_item


class VerificationTests(unittest.TestCase):
    def test_verification_preserves_output_and_rejects_stale_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(pathlib.Path(directory))
            radar.save_archive(settings.archive_path, {"version": 1, "items": [published_item()]})
            radar.build_site(settings)
            verify_generated(settings)
            for name in radar.GENERATED_FILES:
                with self.subTest(artifact=name):
                    path = settings.public_dir / name
                    original = path.read_bytes()
                    path.write_bytes(b"stale output")
                    with self.assertRaisesRegex(radar.RadarError, "stale or nondeterministic"):
                        verify_generated(settings)
                    self.assertEqual(path.read_bytes(), b"stale output")
                    path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
