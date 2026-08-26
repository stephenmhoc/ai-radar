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
