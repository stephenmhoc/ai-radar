from __future__ import annotations

import contextlib
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest

import radar
from scripts.verify import failure_context, run_recording_output, verify_generated
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

    def test_failed_check_reports_the_tail_of_its_output(self) -> None:
        script = "import sys; print('x' * 9000); print('FAIL: test_named_failure'); sys.exit(1)"
        with self.assertRaises(subprocess.CalledProcessError) as caught, \
             contextlib.redirect_stdout(io.StringIO()):
            run_recording_output([sys.executable, "-c", script], {})
        tail = failure_context(caught.exception)["output_tail"]
        self.assertIn("FAIL: test_named_failure", tail)
        self.assertLessEqual(len(tail), 4000)

    def test_failure_without_output_adds_no_context(self) -> None:
        self.assertEqual(failure_context(radar.RadarError("stale")), {})


if __name__ == "__main__":
    unittest.main()
