import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))
import reporting
MODULE = Path(os.environ.get("RETRO2_B5_MODULE", ROOT / "retro2/candidate_adapter.py"))


def load_module():
    spec = importlib.util.spec_from_file_location("retro2_candidate_adapter", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_repo(root):
    repo = Path(root) / "formal"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "src").mkdir()
    (repo / "src/target.txt").write_text("baseline\n")
    subprocess.run(["git", "-C", str(repo), "add", "src/target.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=f@example.invalid", "commit", "-qm", "baseline"],
        check=True,
    )
    return repo


def requirement():
    return {
        "incident_id": "INC-1",
        "diagnosis_id": "diagnosis-INC-1",
        "replay_fixture": "fixtures/INC-1.json",
        "trusted_decision_refs": ["decision-1"],
        "root_cause": {"statement": "duplicate path", "evidence": ["src/caller.py:12"]},
    }


def machine_requirement(content_sha256="a" * 64):
    return {
        "incident_id": "INC-machine",
        "diagnosis_id": "diagnosis-INC-machine",
        "lite": True,
        "trusted_decision_refs": [],
        "machine_evidence_refs": [{
            "source_tool": "hook_audit", "content_sha256": content_sha256,
            "component": "PreToolUse:guard", "symptom": "腳本或命令不存在（exit 127）", "occurrences": 2,
        }],
        "root_cause": {"statement": "stale hook mount", "evidence": ["src/caller.py:12"]},
        "minimal_fix": {"subtractive_first": "remove stale mount", "direction": "repair hook"},
        "decision_links": {"root_cause": [], "minimal_fix": []},
    }


def tree_fingerprint(repo):
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True)
    content = (Path(repo) / "src/target.txt").read_bytes()
    return head, status, hashlib.sha256(content).hexdigest()


class CandidateAdapterTests(unittest.TestCase):
    def test_insights_prose_alone_is_rejected_as_candidate_evidence(self):
        adapter = load_module()
        reference = {
            "source_tool": "insights_prose", "content_sha256": "a" * 64,
            "component": "insights", "symptom": "Suggested CLAUDE.md Additions", "occurrences": 1,
        }
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "evidence").mkdir()
            (run / "evidence" / "incidents.json").write_text(json.dumps({"items": [{
                "source_type": "machine_observed", "machine_evidence_ref": reference,
            }]}), encoding="utf-8")
            requirement = {**machine_requirement(), "machine_evidence_refs": [reference]}
            with self.assertRaises(adapter.CandidateInputError):
                adapter._validated_requirements([requirement], run)

    def test_machine_evidence_must_exist_in_this_run_with_matching_hash(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root)
            evaluator = root / "evaluator"
            evaluator.mkdir(mode=0o500)
            run = root / "run"
            evidence = machine_requirement()
            (run / "evidence").mkdir(parents=True)
            (run / "evidence" / "incidents.json").write_text(json.dumps({"items": [{
                "source_type": "machine_observed", "machine_evidence_ref": evidence["machine_evidence_refs"][0],
            }]}))

            def runner(request):
                Path(request["worktree"], "src/target.txt").write_text(request["anonymous_id"] + "\n")
                return {"diagnosis_ids": ["diagnosis-INC-machine"], "trusted_decision_refs": [],
                        "machine_evidence_refs": evidence["machine_evidence_refs"], "token_usage": {}}

            accepted = adapter.generate_candidates(repo, run, [evidence], ["src/target.txt"], evaluator, runner,
                                                   {"catalog_version": 1, "seed": 1, "timeout_seconds": 1,
                                                    "max_changed_files": 3, "max_changed_lines": 200, "lite": True})
            self.assertEqual("CANDIDATES_DONE", accepted["status"])

            def forged_runner(request):
                Path(request["worktree"], "src/target.txt").write_text(request["anonymous_id"] + "-forged\n")
                forged = {**evidence["machine_evidence_refs"][0], "content_sha256": "b" * 64}
                return {"diagnosis_ids": ["diagnosis-INC-machine"], "trusted_decision_refs": [],
                        "machine_evidence_refs": [*evidence["machine_evidence_refs"], forged], "token_usage": {}}

            forged_run = root / "forged-run"
            (forged_run / "evidence").mkdir(parents=True)
            (forged_run / "evidence" / "incidents.json").write_text((run / "evidence" / "incidents.json").read_text())
            rejected = adapter.generate_candidates(repo, forged_run, [evidence], ["src/target.txt"], evaluator, forged_runner,
                                                   {"catalog_version": 1, "seed": 2, "timeout_seconds": 1,
                                                    "max_changed_files": 3, "max_changed_lines": 200, "lite": True})
            self.assertEqual("NO_CHANGE", rejected["status"])
            self.assertTrue(all("machine_evidence" in json.loads(path.read_text())["reasons"]
                                for path in (forged_run / "candidates").glob("*/manifest.json")))
            with self.assertRaisesRegex(adapter.CandidateInputError, "machine evidence"):
                adapter.generate_candidates(repo, run, [machine_requirement("b" * 64)], ["src/target.txt"], evaluator,
                                            lambda _request: {}, {"catalog_version": 1, "seed": 1, "timeout_seconds": 1,
                                                                   "max_changed_files": 3, "max_changed_lines": 200, "lite": True})
    def test_three_candidates_and_baseline(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root)
            evaluator = root / "evaluator"
            evaluator.mkdir()
            (evaluator / "answers.json").write_text("{}")
            (evaluator / "answers.json").chmod(stat.S_IRUSR)
            evaluator.chmod(stat.S_IRUSR | stat.S_IXUSR)
            before = tree_fingerprint(repo)
            calls = []

            def runner(request):
                worktree = Path(request["worktree"])
                self.assertTrue(worktree.is_dir())
                self.assertNotIn("peer_worktrees", request)
                calls.append(request)
                (worktree / "src/target.txt").write_text(request["anonymous_id"] * 300 + "\n")
                return {
                    "diagnosis_ids": ["diagnosis-INC-1"],
                    "trusted_decision_refs": ["decision-1"],
                    "stdout": "candidate ok",
                    "stderr": "",
                    "token_usage": {"input": 10, "output": 2},
                }

            policy = {"catalog_version": 1, "seed": 42, "timeout_seconds": 30, "max_changed_files": 3, "max_changed_lines": 200}
            result = adapter.generate_candidates(repo, root / "run", [requirement()], ["src/target.txt"], evaluator, runner, policy)
            self.assertEqual("CANDIDATES_DONE", result["status"])
            self.assertEqual(3, len(calls))
            self.assertEqual({"A", "B", "C"}, {call["anonymous_id"] for call in calls})
            self.assertEqual({"minimal_text_correction", "root_cause_logic", "automatic_check_or_test"},
                             {call["strategy"]["name"] for call in calls})
            self.assertEqual(1, len({json.dumps(call["resource_policy"], sort_keys=True) for call in calls}))
            self.assertEqual(3, len({call["worktree"] for call in calls}))
            self.assertEqual(before, tree_fingerprint(repo))
            manifests = sorted((root / "run/candidates").glob("*/manifest.json"))
            self.assertEqual(3, len(manifests))
            self.assertTrue(all(Path(path.parent, "candidate.patch").is_file() for path in manifests))
            ledger = [json.loads(line) for line in (root / "run/evidence-ledger.jsonl").read_text().splitlines()]
            self.assertEqual(["baseline", "candidate", "candidate", "candidate"], [row["kind"] for row in ledger])

    def test_host_target_candidate_isolated_and_hook_change_stays_human_only(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = make_repo(root)
            (host / "hooks").mkdir()
            (host / "hooks/guard.py").write_text("before\n")
            subprocess.run(["git", "-C", str(host), "add", "hooks/guard.py"], check=True)
            subprocess.run(["git", "-C", str(host), "-c", "user.name=fixture", "-c", "user.email=f@example.invalid", "commit", "-qm", "hook"], check=True)
            evaluator = root / "evaluator"
            evaluator.mkdir(mode=0o500)
            host_requirement = {**requirement(), "target": "host"}
            worktrees = []
            def runner(request):
                worktrees.append(Path(request["worktree"]))
                Path(request["worktree"], "hooks/guard.py").write_text(request["anonymous_id"] * 300 + "\n")
                return {"diagnosis_ids": ["diagnosis-INC-1"], "trusted_decision_refs": ["decision-1"]}
            result = adapter.generate_candidates(host, root / "run", [host_requirement], ["hooks/guard.py"], evaluator, runner,
                {"catalog_version": 1, "seed": 1, "timeout_seconds": 1, "max_changed_files": 3, "max_changed_lines": 200, "target": "host"})
            self.assertEqual("CANDIDATES_DONE", result["status"])
            self.assertTrue(all(path != host for path in worktrees))
            manifest = json.loads((root / "run/candidates/A/manifest.json").read_text())
            self.assertEqual("host", manifest["target"])
            self.assertTrue(__import__("importlib").import_module("filter_adapter").classify_security_risks({
                "changed_paths": manifest["changed_paths"], "patch": (root / "run/candidates/A/candidate.patch").read_text(), "host_fail_closed": True,
            }))

    def test_untrusted_or_out_of_allowlist_candidate_is_disqualified(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root)
            evaluator = root / "evaluator"
            evaluator.mkdir(mode=0o500)
            before = tree_fingerprint(repo)
            call_number = 0

            def runner(request):
                nonlocal call_number
                call_number += 1
                worktree = Path(request["worktree"])
                if call_number == 1:
                    (worktree / "outside.txt").write_text("escape\n")
                else:
                    (worktree / "src/target.txt").write_text(f"candidate-{call_number}\n")
                return {
                    "diagnosis_ids": ["diagnosis-INC-1"],
                    "trusted_decision_refs": [] if call_number == 2 else ["decision-1"],
                    "stdout": "",
                    "stderr": "",
                    "token_usage": {},
                }

            result = adapter.generate_candidates(repo, root / "run", [requirement()], ["src/target.txt"], evaluator, runner, {"catalog_version": 1, "seed": 1, "timeout_seconds": 1, "max_changed_files": 3, "max_changed_lines": 200})
            self.assertEqual("NO_CHANGE", result["status"])
            states = [json.loads(path.read_text())["status"] for path in sorted((root / "run/candidates").glob("*/manifest.json"))]
            self.assertEqual(2, states.count("DISQUALIFIED"))
            self.assertEqual(before, tree_fingerprint(repo))

    def test_identical_patches_are_one_substantive_candidate(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root)
            evaluator = root / "evaluator"
            evaluator.mkdir(mode=0o500)

            def runner(request):
                Path(request["worktree"], "src/target.txt").write_text("same\n")
                return {"diagnosis_ids": ["diagnosis-INC-1"], "trusted_decision_refs": ["decision-1"], "stdout": "", "stderr": "", "token_usage": {}}

            result = adapter.generate_candidates(repo, root / "run", [requirement()], ["src/target.txt"], evaluator, runner, {"catalog_version": 1, "seed": 1, "timeout_seconds": 1, "max_changed_files": 3, "max_changed_lines": 200})
            self.assertEqual("NO_CHANGE", result["status"])
            self.assertEqual(1, result["substantive_candidates"])

    def test_near_duplicate_real_run_patches_keep_only_one_candidate(self):
        adapter = load_module()
        fixtures = ROOT / "tests/retro2/fixtures/synthetic-candidates/candidates"
        patches = [
            (fixtures / alias / "candidate.patch").read_text(encoding="utf-8")
            for alias in ("A", "B", "C")
        ]
        self.assertTrue(all(
            adapter._patch_similarity(left, right) > adapter.PATCH_SIMILARITY_LIMIT
            for index, left in enumerate(patches) for right in patches[index + 1:]
        ))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root)
            evaluator = root / "evaluator"
            evaluator.mkdir(mode=0o500)

            def runner(request):
                Path(request["worktree"], "src/target.txt").write_text(
                    "same proposal " + request["anonymous_id"] + "\n"
                )
                return {"diagnosis_ids": ["diagnosis-INC-1"], "trusted_decision_refs": ["decision-1"]}

            result = adapter.generate_candidates(
                repo, root / "run", [requirement()], ["src/target.txt"], evaluator, runner,
                {"catalog_version": 1, "seed": 1, "timeout_seconds": 1,
                 "max_changed_files": 3, "max_changed_lines": 200, "lite": True},
            )
            manifests = [json.loads((root / "run/candidates" / alias / "manifest.json").read_text())
                         for alias in ("A", "B", "C")]
            self.assertEqual("CANDIDATES_DONE", result["status"])
            self.assertEqual(["ELIGIBLE", "DISQUALIFIED", "DISQUALIFIED"], [item["status"] for item in manifests])
            self.assertEqual(["A", "A"], [item["duplicate_of"] for item in manifests[1:]])
            self.assertTrue(all("near_duplicate_patch" in item["reasons"] for item in manifests[1:]))
            report = reporting.write_report(
                reporting.empty_trace("REQUIRES_HUMAN_APPROVAL", "run-fixture"), root / "run/report"
            )
            self.assertIn("改法重複，僅保留", report["markdown"].read_text(encoding="utf-8"))

    def test_writable_evaluator_is_rejected_before_runner(self):
        adapter = load_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root)
            evaluator = root / "evaluator"
            evaluator.mkdir(mode=0o700)
            calls = []
            with self.assertRaisesRegex(adapter.IsolationError, "read-only"):
                adapter.generate_candidates(repo, root / "run", [requirement()], ["src/target.txt"], evaluator, calls.append, {"catalog_version": 1, "seed": 1, "timeout_seconds": 1, "max_changed_files": 3, "max_changed_lines": 200})
            self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()

