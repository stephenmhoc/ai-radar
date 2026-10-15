from __future__ import annotations

import copy
import pathlib
import unittest

import radar
from scripts import check_archive_evolution


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ArchiveEvolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        archive = radar.load_archive(ROOT / "data/items.json")
        cls.first = copy.deepcopy(archive["items"][0])
        cls.second = copy.deepcopy(archive["items"][1])

    def test_append_only_addition_is_allowed(self) -> None:
        before = {"version": 1, "items": [copy.deepcopy(self.first)]}
        after = {"version": 1, "items": [copy.deepcopy(self.first), copy.deepcopy(self.second)]}
        check_archive_evolution.validate_evolution(before, after)

    def test_item_deletion_is_rejected(self) -> None:
        before = {"version": 1, "items": [copy.deepcopy(self.first)]}
        after = {"version": 1, "items": []}
        with self.assertRaisesRegex(radar.RadarError, "removed"):
            check_archive_evolution.validate_evolution(before, after)

    def test_existing_long_summary_change_is_rejected(self) -> None:
        item = next(
            value
            for value in radar.load_archive(ROOT / "data/items.json")["items"]
            if value["long_summary"]
        )
        before_item = copy.deepcopy(item)
        after_item = copy.deepcopy(item)
        after_item["long_summary"] += " Changed."
        with self.assertRaisesRegex(radar.RadarError, "long summary"):
            check_archive_evolution.validate_evolution(
                {"version": 1, "items": [before_item]},
                {"version": 1, "items": [after_item]},
            )


if __name__ == "__main__":
    unittest.main()

    def test_closed_editions_are_immutable(self) -> None:
        item = self.first
        item_id = item["id"]
        edition = {"date": "2026-10-13", "closed_at": "2026-10-13T23:00:00+00:00", "item_ids": [item_id]}
        before = {
            "version": 1,
            "items": [copy.deepcopy(item)],
            "editions": [edition],
            "editions_closed_through": "2026-10-13T23:00:00+00:00",
        }
        if item["status"] != "published":
            self.skipTest("first archive item is not published")
        later = {**before, "editions_closed_through": "2026-10-14T23:00:00+00:00"}
        check_archive_evolution.validate_evolution(before, later)
        with self.assertRaisesRegex(radar.RadarError, "closed daily editions"):
            check_archive_evolution.validate_evolution(before, {**later, "editions": []})
        with self.assertRaisesRegex(radar.RadarError, "closed site days"):
            check_archive_evolution.validate_evolution(
                {**before, "days": [edition], "days_closed_through": "2026-10-14T04:00:00+00:00"},
                {**later, "days": [], "days_closed_through": "2026-10-14T04:00:00+00:00"},
            )
        with self.assertRaisesRegex(radar.RadarError, "moved backward"):
            check_archive_evolution.validate_evolution(
                before, {**before, "editions_closed_through": "2026-10-12T23:00:00+00:00"}
            )
