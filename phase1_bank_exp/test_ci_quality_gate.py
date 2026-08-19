# -*- coding: utf-8 -*-
import ast
import json
from pathlib import Path
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import automated_review, check_policy_baseline  # noqa: E402


class AutomatedReviewContractTest(unittest.TestCase):
    def test_current_repository_passes_structural_review(self):
        issues, tracked_count = automated_review.run_review(REPO_ROOT)
        self.assertGreater(tracked_count, 100)
        self.assertEqual(issues, [])

    def test_conflict_marker_requires_a_complete_conflict_triplet(self):
        self.assertTrue(automated_review.conflict_marker_issue(
            "<<<<<<< ours\na\n=======\nb\n>>>>>>> theirs\n"))
        self.assertFalse(automated_review.conflict_marker_issue(
            "Markdown heading\n=======\n"))

    def test_import_and_wildcard_detection_are_ast_based(self):
        tree = ast.parse("import game\nfrom x import *\n")
        self.assertEqual(automated_review.imported_roots(tree), {"game", "x"})
        self.assertTrue(automated_review.has_wildcard_import(tree))

    def test_policy_output_parser_captures_totals_and_categories(self):
        parsed = check_policy_baseline.parse_output(
            "=== 合計: PASS 1 / FAIL 2 / N/A 3 (分母6件、説明) ===\n"
            "内訳(category別): core: PASS1/FAIL2/N3\n")
        self.assertEqual(parsed, {
            "pass": 1, "fail": 2, "na": 3, "denominator": 6,
            "categories": {"core": {"pass": 1, "fail": 2, "na": 3}},
        })

    def test_baseline_matches_the_versioned_contract(self):
        baseline = json.loads((
            REPO_ROOT / "phase1_bank_exp" /
            "policy_check_baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(baseline["exit_code"], 1)
        self.assertEqual(
            baseline["pass"] + baseline["fail"] + baseline["na"],
            baseline["denominator"])

    def test_workflow_has_safe_automerge_guards(self):
        workflow = (REPO_ROOT / ".github" / "workflows" /
                    "pr-quality.yml").read_text(encoding="utf-8")
        for token in (
                "github.event.pull_request.draft == false",
                "github.event.pull_request.base.ref == github.event.repository.default_branch",
                "github.event.pull_request.head.repo.full_name == github.repository",
                "manual-merge", "do-not-merge", "--match-head-commit",
                "needs: [automated-review, tests]", "EXPECTED_BASE",
                'gh pr checks "$PR_URL" --required'):
            self.assertIn(token, workflow)
        self.assertNotIn("--json mergeStateStatus", workflow)


if __name__ == "__main__":
    unittest.main()
