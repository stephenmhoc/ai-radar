import pathlib
import plistlib
import tempfile
import unittest
from unittest import mock

from podcast_radar import launchd
from podcast_radar.config import AppConfig, Config, LLMConfig, SiteConfig, TranscriptionConfig


class LaunchdTests(unittest.TestCase):
    def test_install_includes_mise_node_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home = root / "home"
            config = Config(
                root=root,
                app=AppConfig(
                    database_path=root / "radar.sqlite3",
                    public_dir=root / "public",
                    state_dir=root / "var",
                ),
                llm=LLMConfig(),
                transcription=TranscriptionConfig(),
                site=SiteConfig(),
                feeds=(),
                labs=(),
            )

            with mock.patch.object(launchd.pathlib.Path, "home", return_value=home):
                target = launchd.install(
                    config,
                    hour=None,
                    minute=0,
                    lookback_hours=2,
                    deploy_project="ai-radar",
                    deploy_branch="main",
                    interval_minutes=60,
                )

            with target.open("rb") as fh:
                plist = plistlib.load(fh)

        path = plist["EnvironmentVariables"]["PATH"].split(":")
        self.assertIn(str(home / ".local" / "share" / "mise" / "shims"), path)
        self.assertIn(str(home / ".local" / "bin"), path)
        self.assertEqual(plist["StartInterval"], 3600)


if __name__ == "__main__":
    unittest.main()
