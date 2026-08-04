import contextlib
import io
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from podcast_radar import cli, storage
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


class CLIConnectionTests(unittest.TestCase):
    def test_commands_close_the_database_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(pathlib.Path(tmp), TranscriptionConfig())
            opened: list[sqlite3.Connection] = []
            real_connect = storage.connect

            def tracking_connect(cfg):
                conn = real_connect(cfg)
                opened.append(conn)
                return conn

            with mock.patch.object(cli.storage, "connect", tracking_connect), \
                    mock.patch.object(cli, "load_config", lambda _path: config), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["list"]), 0)

            self.assertEqual(len(opened), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].execute("SELECT 1")

    def test_serve_site_does_not_open_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(pathlib.Path(tmp), TranscriptionConfig())
            served: list[tuple[str, int]] = []

            with mock.patch.object(cli.storage, "connect", _unexpected_connect), \
                    mock.patch.object(cli, "load_config", lambda _path: config), \
                    mock.patch.object(cli, "serve_site", lambda _c, host, port: served.append((host, port)) or 0):
                self.assertEqual(cli.main(["serve-site", "--port", "9001"]), 0)

            self.assertEqual(served, [("127.0.0.1", 9001)])


def _unexpected_connect(_config):
    raise AssertionError("serve-site should not open the radar database")


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
