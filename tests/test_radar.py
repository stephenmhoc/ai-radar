from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET

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


if __name__ == "__main__":
    unittest.main()
