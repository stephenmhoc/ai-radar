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
        self.assertTrue(all(item["short_summary"] for item in published))
        self.assertTrue(all(item["long_summary"] for item in published))
        self.assertTrue(all(radar.sentence_count(item["short_summary"]) <= 2 for item in published))
        self.assertTrue(all(len(item["short_summary"].split()) <= 55 for item in published))
        summarized = [item for item in archive["items"] if item["long_summary"]]
        self.assertTrue(all(item["short_summary"] for item in summarized))
        self.assertTrue(all(radar.sentence_count(item["short_summary"]) <= 2 for item in summarized))
        self.assertTrue(all(len(item["short_summary"].split()) <= 55 for item in summarized))
        self.assertTrue(all("summary" not in item for item in archive["items"]))
        self.assertEqual(html.count('<li class="episode">'), len(published))
        self.assertEqual(len(rss.findall("./channel/item")), len(published))
        for forbidden in ("<img", "<script", "stylesheet", "episode-card"):
            self.assertNotIn(forbidden, html)
        for expected in ('class="episode-list"', 'class="episode-meta"', 'class="summary"'):
            self.assertIn(expected, html)
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

    def test_site_uses_short_summary_and_rss_uses_long_summary(self) -> None:
        settings = radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json")
        item = {
            "id": "summary-contract",
            "status": "published",
            "title": "Summary contract",
            "published_at": "2026-08-25T12:00:00+00:00",
            "short_summary": "Short site summary.",
            "long_summary": "Long RSS summary with considerably more useful episode detail.",
            "links": {"podcast": "https://example.com/episode"},
        }

        html = radar.render_html(settings, [item])
        rss = ET.fromstring(radar.render_rss(settings, [item]))

        self.assertIn("Short site summary.", html)
        self.assertNotIn("Long RSS summary", html)
        description = rss.findtext("./channel/item/description") or ""
        self.assertIn("Long RSS summary", description)
        self.assertNotIn("Short site summary", description)

    def test_llm_request_requires_strict_structured_output(self) -> None:
        settings = radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json").llm
        structured_value = {
            "include": True,
            "title": "Title",
            "short_summary": "A short summary with enough information to be useful.",
            "long_summary": "A detailed summary " * 15,
            "reason": "Relevant guest.",
        }
        response = FakeHTTPResponse(
            {"choices": [{"message": {"content": json.dumps(structured_value)}}]}
        )
        with (
            mock.patch.dict("os.environ", {settings.api_key_env: "test"}),
            mock.patch.object(radar.urllib.request, "urlopen", return_value=response) as urlopen,
        ):
            value = radar.llm_json(
                settings,
                system="system",
                user="user",
                schema=radar.EDITORIAL_RESPONSE_SCHEMA,
            )

        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(settings.model, "openrouter/auto")
        self.assertEqual(value, structured_value)
        self.assertEqual(payload["model"], "openrouter/auto")
        self.assertEqual(payload["provider"], {"require_parameters": True})
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(
            payload["response_format"]["json_schema"]["schema"],
            radar.EDITORIAL_RESPONSE_SCHEMA,
        )

    def test_existing_long_summary_can_be_shortened_locally(self) -> None:
        long_summary = (
            "The first sentence explains the central idea clearly and directly. "
            "The second sentence supplies one useful supporting detail. "
            "The third sentence should remain exclusive to the RSS feed."
        )
        short_summary = radar.short_summary_from_long(long_summary)

        self.assertEqual(radar.sentence_count(short_summary), 2)
        self.assertNotIn("third sentence", short_summary)

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


class FakeHTTPResponse:
    def __init__(self, value: dict[str, object]) -> None:
        self.body = json.dumps(value).encode()

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


if __name__ == "__main__":
    unittest.main()
