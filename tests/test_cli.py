import pathlib
import tempfile
import unittest

from podcast_radar.cli import transcription_preflight_warnings
from podcast_radar.config import (
    AppConfig,
    Config,
    FeedConfig,
    LLMConfig,
    LabConfig,
    SiteConfig,
    TranscriptionConfig,
)


class CLITests(unittest.TestCase):
    def test_transcription_preflight_accepts_existing_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            model = root / "models" / "ggml-tiny.en.bin"
            model.parent.mkdir()
            model.write_bytes(b"model")

            warnings = transcription_preflight_warnings(
                _config(
                    root,
                    TranscriptionConfig(
                        command="python3",
                        args=("-m", "models/ggml-tiny.en.bin", "-f", "{audio_path}"),
                    ),
                )
            )

        self.assertEqual(warnings, [])

    def test_transcription_preflight_warns_for_missing_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            warnings = transcription_preflight_warnings(
                _config(
                    root,
                    TranscriptionConfig(
                        command="python3",
                        args=("--model", "models/missing.bin", "-f", "{audio_path}"),
                    ),
                )
            )

        self.assertEqual(len(warnings), 1)
        self.assertIn("transcription model not found:", warnings[0])


def _config(root: pathlib.Path, transcription: TranscriptionConfig) -> Config:
    return Config(
        root=root,
        app=AppConfig(database_path=root / "radar.sqlite3", public_dir=root / "public", state_dir=root),
        llm=LLMConfig(),
        transcription=transcription,
        site=SiteConfig(),
        feeds=(FeedConfig(name="Example", url="https://example.com/feed"),),
        labs=(LabConfig(name="OpenAI"),),
    )


if __name__ == "__main__":
    unittest.main()
