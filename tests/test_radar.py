from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from unittest import mock

import radar


ROOT = pathlib.Path(__file__).resolve().parents[1]


class StaticPublisherTests(unittest.TestCase):
    def test_production_archive_and_outputs_match(self) -> None:
        archive = json.loads((ROOT / "data/items.json").read_text(encoding="utf-8"))
        published = [item for item in archive["items"] if item["status"] == "published"]
        html = (ROOT / "public/index.html").read_text(encoding="utf-8")
        rss = ET.parse(ROOT / "public/feed.xml")

        self.assertEqual(archive["version"], 1)
        self.assertGreaterEqual(len(archive["items"]), 1734)
        self.assertGreaterEqual(len(published), 222)
        self.assertTrue(all(item["links"] for item in published))
        self.assertTrue(all(set(item["links"]) <= {"podcast", "youtube"} for item in published))
        self.assertEqual(html.count("<li>"), len(published))
        self.assertEqual(len(rss.findall("./channel/item")), len(published))
        for forbidden in ("<img", "<script", "stylesheet", "episode-card"):
            self.assertNotIn(forbidden, html)
        self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", html))

    def test_build_site_is_deterministic(self) -> None:
        settings = radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json")
        with tempfile.TemporaryDirectory() as directory:
            settings = radar.Settings(**{**settings.__dict__, "public_dir": pathlib.Path(directory)})
            stats = radar.build_site(settings)
            published_count = len(
                [
                    item
                    for item in json.loads((ROOT / "data/items.json").read_text(encoding="utf-8"))["items"]
                    if item["status"] == "published"
                ]
            )
            self.assertEqual(stats, {"items": published_count, "rss_items": published_count})
            self.assertEqual(
                (pathlib.Path(directory) / "index.html").read_bytes(),
                (ROOT / "public/index.html").read_bytes(),
            )

    def test_group_candidates_deduplicates_matching_cross_posts(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        first = {"title": "Building Useful AI Agents", "published_at": now, "family": "show"}
        second = {"title": "Building Useful AI Agents — Full Episode", "published_at": now, "family": "show"}
        self.assertEqual(len(radar.group_candidates([first, second])), 1)

    def test_source_failure_is_reported_without_stopping_the_cycle(self) -> None:
        reporter = RecordingReporter()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            settings = replace(
                radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json"),
                archive_path=root / "items.json",
                public_dir=root / "public",
                sources=(
                    radar.Source(
                        kind="podcast",
                        name="Broken Show",
                        feed_url="https://example.com/broken.xml",
                        homepage_url="https://example.com/",
                        family="broken show",
                    ),
                ),
            )
            with (
                mock.patch.dict("os.environ", {settings.llm.api_key_env: "test"}),
                mock.patch.object(radar, "fetch_bytes", side_effect=TimeoutError("timed out")),
            ):
                stats = radar.run_cycle(settings, lookback_days=7, reporter=reporter)

        self.assertEqual(stats["source_errors"], 1)
        self.assertEqual(len(reporter.exceptions), 1)
        self.assertEqual(reporter.exceptions[0]["tags"]["source"], "Broken Show")
        self.assertEqual(
            reporter.exceptions[0]["fingerprint"],
            ["ai-radar", "source", "podcast", "Broken Show"],
        )

    def test_summary_failure_is_reported_and_left_for_retry(self) -> None:
        reporter = RecordingReporter()
        published = email_date(dt.datetime.now(dt.timezone.utc))
        feed = f"""<rss><channel><item>
          <title>New AI episode</title>
          <description>{'Useful publisher notes. ' * 10}</description>
          <link>https://example.com/episode</link>
          <pubDate>{published}</pubDate>
        </item></channel></rss>""".encode()
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            settings = replace(
                radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json"),
                archive_path=root / "items.json",
                public_dir=root / "public",
                sources=(
                    radar.Source(
                        kind="podcast",
                        name="Test Show",
                        feed_url="https://example.com/feed.xml",
                        homepage_url="https://example.com/",
                        family="test show",
                    ),
                ),
            )
            with (
                mock.patch.dict("os.environ", {settings.llm.api_key_env: "test"}),
                mock.patch.object(radar, "fetch_bytes", return_value=feed),
                mock.patch.object(radar, "summarize_group", side_effect=radar.RadarError("provider down")),
            ):
                stats = radar.run_cycle(settings, lookback_days=7, reporter=reporter)

        self.assertEqual(stats["llm_errors"], 1)
        self.assertEqual(stats["new_items"], 0)
        self.assertEqual(len(reporter.exceptions), 1)
        self.assertEqual(reporter.exceptions[0]["tags"]["phase"], "summary")


def email_date(value: dt.datetime) -> str:
    import email.utils

    return email.utils.format_datetime(value)


class RecordingReporter:
    def __init__(self) -> None:
        self.exceptions: list[dict[str, object]] = []

    def capture_exception(self, exception: BaseException, **context: object) -> None:
        self.exceptions.append({"exception": exception, **context})


if __name__ == "__main__":
    unittest.main()
