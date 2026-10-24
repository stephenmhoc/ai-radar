from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

import editorial
import radar
import radar_common
from scripts import eval_editorial
from tests.test_radar import ROOT, VALID_LONG, VALID_SHORT, FakeHTTPResponse


def response(include: bool, followed_people: list[str], *, cost: float = 0.001) -> FakeHTTPResponse:
    value = {
        "include": include,
        "title": "Title",
        "short_summary": VALID_SHORT if include else "",
        "long_summary": VALID_LONG if include else "",
        "reason": "Judged.",
        "followed_people": followed_people,
    }
    return FakeHTTPResponse(
        {
            "model": "vendor/model",
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": cost},
            "choices": [{"message": {"content": json.dumps(value)}}],
        }
    )


class EditorialEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = radar.load_settings(ROOT / "config.toml", ROOT / "data/items.json")
        cls.archive = radar.load_archive(cls.settings.archive_path)
        cls.cases = {case["id"]: case for case in eval_editorial.load_cases(cls.settings, cls.archive)}

    def run_case(self, case_id: str, *responses: FakeHTTPResponse) -> dict[str, object]:
        with (
            mock.patch.dict("os.environ", {self.settings.llm.api_key_env: "test"}),
            mock.patch.object(editorial.urllib.request, "urlopen", side_effect=list(responses)),
            mock.patch.object(radar.time, "sleep"),
        ):
            outcome = eval_editorial.run_case(self.settings, self.cases[case_id])
        return eval_editorial.grade(self.settings, self.cases[case_id], outcome)

    def test_cases_cover_every_category_with_valid_labels(self) -> None:
        categories = {case["category"] for case in self.cases.values()}
        self.assertEqual(categories, set(eval_editorial.CATEGORIES))
        for case in self.cases.values():
            with self.subTest(case=case["id"]):
                self.assertTrue(case["appearances"])
                self.assertEqual(case["expect"] == "include", bool(case["followed_people"]))

    def test_synthetic_cases_carry_their_own_appearances(self) -> None:
        synthetic = [case for case in self.cases.values() if case["id"].startswith("synthetic-")]
        self.assertTrue(synthetic)
        for case in synthetic:
            with self.subTest(case=case["id"]):
                self.assertTrue(case["appearances"][0]["description"])

    def test_invalid_labels_are_rejected(self) -> None:
        archive = self.archive
        item_id = next(iter(self.cases))
        bad = [
            {"id": "missing", "expect": "exclude", "followed_people": [], "category": "clip", "note": ""},
            {"id": item_id, "expect": "include", "followed_people": ["Jacob Tsimerman"], "category": "guest", "note": ""},
            {"id": item_id, "expect": "include", "followed_people": [], "category": "guest", "note": ""},
            {"id": "synthetic-x", "expect": "exclude", "followed_people": [], "category": "clip", "note": ""},
            {"id": item_id, "expect": "exclude", "followed_people": [], "category": "clip", "note": "", "appearances": []},
        ]
        for case in bad:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                path = pathlib.Path(directory) / "cases.json"
                path.write_text(json.dumps({"cases": [case]}))
                with self.assertRaises(radar_common.RadarError):
                    eval_editorial.load_cases(self.settings, archive, path)

    def test_correct_inclusion_records_people_tokens_and_cost(self) -> None:
        graded = self.run_case("17cf848671f9d696efea", response(True, ["Sam Altman"]))
        self.assertTrue(graded["correct"])
        self.assertTrue(graded["raw_correct"])
        self.assertTrue(graded["people_match"])
        self.assertEqual((graded["attempts"], graded["prompt_tokens"], graded["completion_tokens"]), (1, 100, 20))
        self.assertEqual(graded["routed_models"], ["vendor/model"])

    def test_raw_model_mistake_is_scored_even_when_local_check_rescues_it(self) -> None:
        graded = self.run_case("4a1f59324f42381ba774", response(True, ["Jacob Tsimerman"]))
        self.assertEqual(graded["status"], "skipped")
        self.assertTrue(graded["correct"])
        self.assertFalse(graded["raw_correct"])

    def test_missed_inclusion_and_wrong_people_are_misses(self) -> None:
        missed = self.run_case("17cf848671f9d696efea", response(False, []))
        self.assertFalse(missed["correct"])
        partial = self.run_case("e398eb71f41e5eafe3bb", response(True, ["Zuck"]))
        self.assertTrue(partial["correct"])
        self.assertFalse(partial["people_match"])

    def test_alias_counts_as_the_expected_person(self) -> None:
        graded = self.run_case("9ca02aebbae30bbf20f8", response(True, ["Tibo"]))
        self.assertTrue(graded["correct"])
        self.assertTrue(graded["people_match"])

    def test_summary_and_exit_status_report_misses(self) -> None:
        graded = [
            self.run_case("17cf848671f9d696efea", response(True, ["Sam Altman"], cost=0.002)),
            self.run_case("04d9ed66dc2c002e96df", response(False, [])),
        ]
        summary = eval_editorial.summarize(graded)
        self.assertEqual((summary["cases"], summary["correct"], summary["false_includes"]), (2, 2, 0))
        self.assertEqual(summary["cost"], 0.003)
        report = eval_editorial.render([{"model": "m", "summary": summary, "cases": graded}])
        self.assertNotIn("MISS", report)

    def test_omitted_temperature_is_not_sent(self) -> None:
        case = self.cases["17cf848671f9d696efea"]
        for omit, expected in ((False, True), (True, False)):
            with (
                self.subTest(omit=omit),
                mock.patch.dict("os.environ", {self.settings.llm.api_key_env: "test"}),
                mock.patch.object(
                    editorial.urllib.request, "urlopen", return_value=response(True, ["Sam Altman"])
                ) as urlopen,
            ):
                settings = replace(self.settings, llm=replace(self.settings.llm, temperature=0.1))
                result = eval_editorial.evaluate(settings, [case], "vendor/model", 1, omit_temperature=omit)
            payload = json.loads(urlopen.call_args.args[0].data)
            self.assertEqual(payload["model"], "vendor/model")
            self.assertEqual("temperature" in payload, expected)
            self.assertEqual(result["summary"]["raw_correct"], "1/1")

    def test_locally_decided_cases_are_left_out_of_the_raw_score(self) -> None:
        local = next(
            case for case in self.cases.values()
            if all(radar_common.is_youtube_short(value) for value in case["appearances"])
        )
        with mock.patch.object(editorial.urllib.request, "urlopen") as urlopen:
            outcome = eval_editorial.run_case(self.settings, local)
        urlopen.assert_not_called()
        graded = eval_editorial.grade(self.settings, local, outcome)
        self.assertTrue(graded["local"])
        summary = eval_editorial.summarize([graded])
        self.assertEqual((summary["decided_locally"], summary["raw_correct"]), (1, "0/0"))

    def test_requires_api_key_before_any_request(self) -> None:
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.object(editorial.urllib.request, "urlopen") as urlopen,
            mock.patch("sys.stderr"),
        ):
            self.assertEqual(eval_editorial.main([]), 2)
        urlopen.assert_not_called()

    def test_observer_sees_every_attempt_before_validation(self) -> None:
        seen: list[dict[str, object]] = []
        llm = radar_common.LLMSettings(**{**self.settings.llm.__dict__, "max_attempts": 2})
        with (
            mock.patch.dict("os.environ", {llm.api_key_env: "test"}),
            mock.patch.object(
                editorial.urllib.request,
                "urlopen",
                side_effect=[response(True, ["Sam Altman"]), response(False, [])],
            ),
            mock.patch.object(radar.time, "sleep"),
        ):
            def reject_first(value: dict[str, object]) -> dict[str, object]:
                if len(seen) == 1:
                    raise radar_common.RadarError("LLM structured response failed local validation: test")
                return value

            result = editorial.llm_json(
                llm, system="s", user="u", schema=editorial.EDITORIAL_RESPONSE_SCHEMA,
                validator=reject_first, observer=seen.append,
            )
        self.assertEqual([value["response"]["include"] for value in seen], [True, False])
        self.assertFalse(result["include"])
        self.assertEqual(seen[0]["usage"]["prompt_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
