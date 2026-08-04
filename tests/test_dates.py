import datetime as dt
import unittest

from podcast_radar import dates


class ParseTests(unittest.TestCase):
    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        self.assertEqual(
            dates.parse_datetime("2026-08-01T12:00:00"),
            dt.datetime(2026, 8, 1, 12, tzinfo=dt.timezone.utc),
        )

    def test_offsets_are_normalized_to_utc(self) -> None:
        self.assertEqual(
            dates.parse_datetime("2026-08-01T08:00:00-04:00"),
            dt.datetime(2026, 8, 1, 12, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(
            dates.parse_datetime("2026-08-01T12:00:00Z"),
            dt.datetime(2026, 8, 1, 12, tzinfo=dt.timezone.utc),
        )

    def test_unparseable_values_are_none(self) -> None:
        self.assertIsNone(dates.parse_datetime("last Tuesday"))
        self.assertIsNone(dates.parse_datetime(""))
        self.assertIsNone(dates.parse_datetime(None))

    def test_cutoff_accepts_a_bare_date(self) -> None:
        self.assertEqual(
            dates.parse_cutoff("2026-01-01"),
            dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        self.assertIsNone(dates.parse_cutoff(None))

    def test_undated_items_survive_the_cutoff(self) -> None:
        cutoff = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)

        self.assertTrue(dates.is_since(None, cutoff))
        self.assertTrue(dates.is_since("who knows", cutoff))
        self.assertTrue(dates.is_since("2026-01-01T00:00:00+00:00", cutoff))
        self.assertFalse(dates.is_since("2025-12-31T23:59:59+00:00", cutoff))


class DurationTests(unittest.TestCase):
    def test_reads_every_shape_the_collectors_produce(self) -> None:
        self.assertEqual(dates.duration_seconds("01:02:03"), 3723)
        self.assertEqual(dates.duration_seconds("02:03"), 123)
        self.assertEqual(dates.duration_seconds("3723"), 3723)
        self.assertEqual(dates.duration_seconds(3723), 3723)
        self.assertEqual(dates.duration_seconds(3723.4), 3723)
        self.assertEqual(dates.duration_seconds("PT1H2M3S"), 3723)
        self.assertEqual(dates.duration_seconds("PT45M"), 2700)

    def test_iso_days_are_not_dropped(self) -> None:
        self.assertEqual(dates.duration_seconds("P1DT1H"), 90000)

    def test_unreadable_durations_are_none(self) -> None:
        for value in (None, "", "  ", "about an hour", True):
            with self.subTest(value=value):
                self.assertIsNone(dates.duration_seconds(value))

    def test_close_durations_tolerate_cross_post_drift(self) -> None:
        self.assertTrue(dates.durations_close("01:02:00", "01:01:30"))
        self.assertTrue(dates.durations_close("01:02:00", 3720))
        self.assertFalse(dates.durations_close("01:02:00", "00:31:00"))
        self.assertFalse(dates.durations_close("01:02:00", None))

    def test_short_clips_use_the_two_minute_floor(self) -> None:
        self.assertTrue(dates.durations_close("05:00", "06:30"))
        self.assertFalse(dates.durations_close("05:00", "08:00"))


class ProximityTests(unittest.TestCase):
    def test_publication_windows_compare_across_formats(self) -> None:
        self.assertTrue(dates.datetimes_close("2026-08-01T12:00:00Z", "2026-08-03T12:00:00+00:00", days=4))
        self.assertFalse(dates.datetimes_close("2026-08-01T12:00:00Z", "2026-08-09T12:00:00+00:00", days=4))
        self.assertFalse(dates.datetimes_close("2026-08-01T12:00:00Z", None, days=4))


if __name__ == "__main__":
    unittest.main()
