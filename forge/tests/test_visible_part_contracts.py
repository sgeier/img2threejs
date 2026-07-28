from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "_shared"))
sys.path.insert(0, str(ROOT / "stage4_review"))

from append_review import main as append_review_main  # noqa: E402
from visible_part_contracts import (  # noqa: E402
    evaluate_visible_part_report,
    stamp_visible_part_report,
    visible_part_spec_gaps,
)


class VisiblePartContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.spec = json.loads(
            (FIXTURES / "mini_visible_part_spec.json").read_text(encoding="utf-8")
        )
        self.bad = json.loads(
            (FIXTURES / "mini_visible_part_reviews_bad.json").read_text(encoding="utf-8")
        )
        self.good = json.loads(
            (FIXTURES / "mini_visible_part_reviews_good.json").read_text(encoding="utf-8")
        )
        parts = self.root / "parts"
        parts.mkdir()
        for name in ("wipers", "windshield", "grille", "nose", "mirror", "door"):
            (parts / f"{name}.ts").write_text(
                f"export const {name}Revision = 1;\n", encoding="utf-8"
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_known_mini_failures_block_despite_passing_local_scores(self) -> None:
        report = stamp_visible_part_report(
            self.spec, "form-refinement", self.bad, self.root
        )

        self.assertEqual(report["verdict"], "fail")
        failures = report["failedGates"]
        self.assertTrue(any("windscreen-wipers:wrong-parent" in item for item in failures))
        self.assertTrue(any("windscreen-wipers:pose-delta" in item for item in failures))
        self.assertTrue(
            any("front-grille:defect-wrong-change:mini-grille-contour" in item for item in failures)
        )
        self.assertTrue(any("left-door-mirror:wrong-parent" in item for item in failures))
        self.assertTrue(any("left-door-mirror:pose-delta" in item for item in failures))

    def test_corrected_wipers_grille_and_mirror_pass(self) -> None:
        report = stamp_visible_part_report(
            self.spec, "form-refinement", self.good, self.root
        )

        self.assertEqual(report["verdict"], "pass", report["failedGates"])
        verified = evaluate_visible_part_report(
            self.spec, report, "form-refinement", self.root
        )
        self.assertTrue(verified["passed"], verified["failedGates"])

    def test_parent_geometry_change_invalidates_child_review(self) -> None:
        report = stamp_visible_part_report(
            self.spec, "form-refinement", self.good, self.root
        )
        (self.root / "parts" / "windshield.ts").write_text(
            "export const windshieldRevision = 2;\n", encoding="utf-8"
        )

        verified = evaluate_visible_part_report(
            self.spec, report, "form-refinement", self.root
        )

        self.assertFalse(verified["passed"])
        self.assertIn(
            "part:windscreen-wipers:stale-dependency:parts/windshield.ts",
            verified["failedGates"],
        )

    def test_contract_change_invalidates_previous_review(self) -> None:
        report = stamp_visible_part_report(
            self.spec, "form-refinement", self.good, self.root
        )
        self.spec["visiblePartContracts"][1]["requiredChecks"].append("overlap")

        verified = evaluate_visible_part_report(
            self.spec, report, "form-refinement", self.root
        )

        self.assertFalse(verified["passed"])
        self.assertIn("visible-part-contracts-stale", verified["failedGates"])

    def test_important_component_without_contract_is_a_spec_gap(self) -> None:
        self.spec["visiblePartContracts"] = self.spec["visiblePartContracts"][:-1]

        gaps = visible_part_spec_gaps(self.spec, "form-refinement")

        self.assertIn(
            "important visible component 'left-door-mirror' has no visible-part contract",
            gaps,
        )

    def test_append_review_requires_and_persists_passing_report(self) -> None:
        spec_path = self.root / "spec.json"
        report_path = self.root / "visible-report.json"
        render = self.root / "render.png"
        comparison = self.root / "comparison.png"
        render.write_bytes(b"render")
        comparison.write_bytes(b"comparison")
        spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        report = stamp_visible_part_report(
            self.spec, "form-refinement", self.good, self.root
        )
        report_path.write_text(json.dumps(report), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "visible-part-report-json"):
            append_review_main(
                [
                    str(spec_path),
                    "--pass-id", "form-refinement",
                    "--fidelity", "0.9",
                    "--action", "continue",
                    "--summary", "global score alone is insufficient",
                    "--render-screenshot", str(render),
                    "--comparison-image", str(comparison),
                    "--ai-vision-score", "0.95",
                ]
            )

        result = append_review_main(
            [
                str(spec_path),
                "--pass-id", "form-refinement",
                "--fidelity", "0.9",
                "--action", "continue",
                "--summary", "all local Mini contracts pass",
                "--render-screenshot", str(render),
                "--comparison-image", str(comparison),
                "--ai-vision-score", "0.95",
                "--visible-part-report-json", str(report_path),
                "--in-place",
            ]
        )

        self.assertEqual(result, 0)
        persisted = json.loads(spec_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["reviewHistory"][0]["visiblePartReport"]["verdict"], "pass"
        )
        self.assertEqual(persisted["sculptPipeline"]["currentPass"], "complete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
