from __future__ import annotations

import itertools
import pathlib
import tempfile
import unittest
from unittest import mock

import radar
from tests.test_radar import (
    NOW,
    VALID_LONG, VALID_SHORT, RecordingReporter, make_appearance, make_settings,
    make_source, published_item,
)


RESULT = {
    "status": "published", "title": "Building Useful AI Agents",
    "short_summary": VALID_SHORT, "long_summary": VALID_LONG, "reason": "Relevant guest.",
}


class MatchingRegressionTests(unittest.TestCase):
    def test_three_appearances_preserve_distinct_videos_in_every_order(self) -> None:
        appearances = [make_appearance(), make_appearance(kind="youtube", guid="video111"),
                       make_appearance(kind="youtube", guid="video222")]
        expected = {radar.media_identity(value) for value in appearances}
        for values in itertools.permutations(appearances):
            with self.subTest(order=[value["guid"] for value in values]):
                groups = radar.group_candidates(list(values))
                kept = [value for group in groups for value in radar.canonicalize_group(group)]
                self.assertEqual({radar.media_identity(value) for value in kept}, expected)
                self.assertEqual(len(kept), 3)
                self.assertTrue(all(len({v["kind"] for v in group}) == len(group) for group in groups))

    def test_duplicate_video_from_another_source_enriches_existing_group(self) -> None:
        podcast = make_appearance()
        video = make_appearance(kind="youtube", guid="video111")
        richer = make_appearance(kind="youtube", guid="video111", source_name="Playlist",
                                 description="Richer notes. " * 100)
        groups = radar.group_candidates([podcast, video, richer])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)
        retained = next(value for value in groups[0] if value["kind"] == "youtube")
        self.assertEqual(retained["description"], richer["description"])

    def test_archived_short_cannot_absorb_a_podcast(self) -> None:
        item = published_item()
        item["appearances"] = [make_appearance(kind="youtube", guid="short111",
                                               url="https://www.youtube.com/shorts/short111")]
        self.assertIsNone(radar.matching_item({"items": [item]}, make_appearance()))


class DeferredRegressionTests(unittest.TestCase):
    def test_failed_refresh_remains_retryable_for_notes_and_new_medium(self) -> None:
        for new_medium in (False, True):
            with self.subTest(new_medium=new_medium), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                source = make_source(kind="youtube" if new_medium else "podcast")
                settings = make_settings(root, sources=(source,))
                item = published_item()
                item.update(status="deferred", short_summary="", long_summary="")
                item["appearances"][0]["description"] = "Sparse notes."
                archive = {"version": 1, "items": [item]}
                radar.close_editions(settings, archive, NOW)
                radar.close_days(settings, archive, NOW)
                radar.save_archive(settings.archive_path, archive)
                before = settings.archive_path.read_bytes()
                candidate = make_appearance(kind=source.kind,
                                            guid="video111" if new_medium else "episode-1")
                with mock.patch.object(radar, "fetch_bytes", return_value=b"fixture"), \
                     mock.patch.object(radar, "parse_feed", return_value=[candidate]):
                    with mock.patch.object(radar, "summarize_group", side_effect=radar.RadarError("provider down")):
                        failed = radar.run_cycle(settings, lookback_days=7, now=NOW)
                    self.assertEqual(failed["llm_errors"], 1)
                    self.assertEqual(settings.archive_path.read_bytes(), before)
                    with mock.patch.object(radar, "summarize_group", return_value=RESULT) as summarize:
                        recovered = radar.run_cycle(settings, lookback_days=7, now=NOW)
                    summarize.assert_called_once()
                self.assertEqual(recovered["reevaluated"], 1)
                saved = radar.load_archive(settings.archive_path)["items"][0]
                self.assertEqual(saved["status"], "published")
                self.assertEqual(len(saved["appearances"]), 2 if new_medium else 1)

    def test_unchanged_sparse_notes_do_not_repeat_editorial_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = make_settings(pathlib.Path(directory), sources=(make_source(),))
            candidate = make_appearance(description="Sparse notes.")
            with mock.patch.object(radar, "fetch_bytes", return_value=b"fixture"), \
                 mock.patch.object(radar, "parse_feed", return_value=[candidate]):
                radar.run_cycle(settings, lookback_days=7)
                with mock.patch.object(radar, "summarize_group") as summarize:
                    radar.run_cycle(settings, lookback_days=7)
                summarize.assert_not_called()


    def test_same_video_from_two_sources_in_one_run_has_one_owner(self) -> None:
        # Production 2026-10-14: a channel feed and a show playlist both carried one new
        # video. Only the playlist copy cleared the fuzzy threshold (its family matched the
        # podcast), so the channel copy became a second owner of the same video and archive
        # validation aborted every hourly cycle. Replays the production titles and families.
        podcast_title = "Can Open Models Keep Up With the AI Frontier? | Nathan Lambert"
        video_title = "Can Open Models Keep Up With OpenAI and Anthropic? | Nathan Lambert"
        for channel_first in (True, False):
            with self.subTest(channel_first=channel_first), tempfile.TemporaryDirectory() as directory:
                channel = make_source(kind="youtube", name="Channel")
                playlist = make_source(kind="youtube", name="Show — YouTube")
                sources = (channel, playlist) if channel_first else (playlist, channel)
                settings = make_settings(pathlib.Path(directory), sources=sources)
                item = published_item()
                item["appearances"] = [make_appearance(title=podcast_title)]
                item["source_title"] = podcast_title
                item["links"] = radar.source_links(item["appearances"], preferred_title=podcast_title)
                radar.save_archive(settings.archive_path, {"version": 1, "items": [item]})
                channel_copy = make_appearance(kind="youtube", source_name="Channel", guid="video111",
                                               title=video_title, description="Short channel notes. " * 6)
                channel_copy["family"] = "channel"
                playlist_copy = make_appearance(kind="youtube", source_name="Show — YouTube", guid="video111",
                                                title=video_title,
                                                description="Much fuller playlist notes about the episode. " * 8)
                self.assertLess(radar.matching_score(item["appearances"], channel_copy, title=podcast_title), 0.84)
                self.assertGreater(radar.matching_score(item["appearances"], playlist_copy, title=podcast_title), 0.84)
                copies = {channel.feed_url: [channel_copy], playlist.feed_url: [playlist_copy]}
                with mock.patch.object(radar, "fetch_bytes", side_effect=lambda url, **_: url.encode()), \
                     mock.patch.object(radar, "parse_feed", side_effect=lambda data, source: copies[data.decode()]), \
                     mock.patch.object(radar, "summarize_group", return_value=RESULT) as summarize:
                    stats = radar.run_cycle(settings, lookback_days=7)
                summarize.assert_not_called()
                items = radar.load_archive(settings.archive_path)["items"]
                self.assertEqual(stats["new_items"], 0)
                self.assertEqual(len(items), 1)
                video = next(value for value in items[0]["appearances"] if value["kind"] == "youtube")
                self.assertIn("fuller playlist notes", video["description"])

class ReportingRegressionTests(unittest.TestCase):
    def test_other_failures_report_during_new_and_continuing_youtube_outages(self) -> None:
        youtube = tuple(make_source(kind="youtube", name=f"Channel {index}") for index in range(3))
        podcast = make_source(name="Podcast")
        metadata_source = make_source(kind="youtube", name="Undated channel")
        for already_alerted in (False, True):
            with self.subTest(already_alerted=already_alerted), tempfile.TemporaryDirectory() as directory:
                settings = make_settings(pathlib.Path(directory), sources=youtube + (podcast, metadata_source))
                if already_alerted:
                    latch = settings.public_dir.parent / "var/youtube-rss-outage-alerted"
                    latch.parent.mkdir()
                    latch.write_text("prior-event\n")
                reporter = RecordingReporter()
                def fetch(url: str, **kwargs: object) -> bytes:
                    if url == metadata_source.feed_url:
                        return b"<rss><channel><item><title>Undated entry</title></item></channel></rss>"
                    raise radar.RadarError("fixture fetch failure")
                with mock.patch.object(radar, "fetch_bytes", side_effect=fetch), \
                     mock.patch.object(radar.time, "sleep"):
                    stats = radar.run_cycle(settings, lookback_days=7, reporter=reporter)
                self.assertEqual(stats["source_errors"], 5)
                ordinary = [event for event in reporter.exceptions
                            if event["fingerprint"] == ["ai-radar", "source", "cycle"]]
                self.assertEqual(len(ordinary), 1)
                self.assertEqual({value["source"] for value in ordinary[0]["extra"]["failures"]},
                                 {podcast.name, metadata_source.name})
                self.assertEqual(len(reporter.exceptions), 1 if already_alerted else 2)


if __name__ == "__main__":
    unittest.main()
