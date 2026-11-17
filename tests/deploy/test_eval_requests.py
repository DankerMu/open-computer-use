# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2026 Open Computer Use Contributors
"""The evaluation request set: its shape, its counts and the prompts' wording rules."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
REQUESTS = ROOT / "deploy" / "eval" / "requests.json"

ID_PATTERN = re.compile(r"[a-z0-9-]{3,40}")
KIND_COUNTS = {"explainer": 5, "course": 1, "promo": 1, "report": 1}
TOTAL_RUNS = 13
FORBIDDEN = ("remotion", "kit", "skill", "技能", "/")


def load_requests() -> dict:
    return json.loads(REQUESTS.read_text(encoding="utf-8"))


class EvalRequestSetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = load_requests()
        cls.requests = cls.data["requests"]

    def test_top_level_shape(self) -> None:
        self.assertEqual(set(self.data), {"version", "requests"})
        self.assertIs(type(self.data["version"]), int)
        self.assertEqual(self.data["version"], 1)
        self.assertIsInstance(self.requests, list)
        for request in self.requests:
            self.assertEqual(set(request), {"id", "kind", "repetitions", "prompt"}, request)

    def test_ids_are_well_formed_and_unique(self) -> None:
        ids = [request["id"] for request in self.requests]
        for request_id in ids:
            self.assertIsInstance(request_id, str)
            self.assertIsNotNone(ID_PATTERN.fullmatch(request_id), request_id)
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_kind_counts(self) -> None:
        self.assertEqual(Counter(request["kind"] for request in self.requests), KIND_COUNTS)

    def test_repetitions_and_total_runs(self) -> None:
        for request in self.requests:
            expected = 2 if request["kind"] == "explainer" else 1
            self.assertIs(type(request["repetitions"]), int, request["id"])
            self.assertEqual(request["repetitions"], expected, request["id"])
        self.assertEqual(sum(request["repetitions"] for request in self.requests), TOTAL_RUNS)

    def test_prompt_length(self) -> None:
        for request in self.requests:
            prompt = request["prompt"]
            self.assertIsInstance(prompt, str, request["id"])
            self.assertGreaterEqual(len(prompt), 20, request["id"])
            self.assertLessEqual(len(prompt), 200, request["id"])

    def test_prompts_name_no_tool_skill_or_path(self) -> None:
        for request in self.requests:
            lowered = request["prompt"].lower()
            for word in FORBIDDEN:
                self.assertNotIn(word, lowered, request["id"])


if __name__ == "__main__":
    unittest.main()
