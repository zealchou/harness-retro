import hashlib
import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))
experience = importlib.import_module("experience")
station_host = importlib.import_module("station_host")
candidate_adapter = importlib.import_module("candidate_adapter")


def make_repo(root, name):
    repo = Path(root) / name
    (repo / "docs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    (repo / "x.py").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "x.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    return repo


def finding(incident, *, secret="ordinary symptom"):
    return {
        "target": "subject",
        "statement": "A repeated guard failure needs a root-cause repair.",
        "angles": ["Automated checks", "No-ops"],
        "strategy_name": "root_cause_logic",
        "machine_evidence_refs": [{
            "content_sha256": hashlib.sha256(incident.encode()).hexdigest(),
            "component": "PreToolUse:guard", "symptom": secret,
        }],
    }


def record(repo, global_path, projects, incident, session, run=None, secret="ordinary symptom"):
    return experience.record_win(
        repo, run_id=run or "run-" + incident, session_id=session,
        finding=finding(incident, secret=secret), commit_sha="a" * 40,
        global_path=global_path, project_repos=projects,
    )


def documents(path):
    return experience._read_entries(path)


class ExperienceStateTests(unittest.TestCase):
    def test_same_incident_rerun_stays_provisional(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_repo(directory, "project")
            global_path = Path(directory) / "global.md"
            first = record(repo, global_path, [repo], "incident-1", "session-1", "run-1")
            second = record(repo, global_path, [repo], "incident-1", "session-1", "run-2")
            entry = documents(repo / "docs/EXPERIENCE.md")[first["experience_id"]]
            self.assertEqual("provisional", entry["status"])
            self.assertEqual((1, 1), (entry["confidence"]["wins"], entry["confidence"]["independent_incidents"]))
            self.assertEqual("duplicate_incident", second["action"])

    def test_second_independent_incident_and_session_confirms(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_repo(directory, "project")
            global_path = Path(directory) / "global.md"
            first = record(repo, global_path, [repo], "incident-1", "session-1")
            result = record(repo, global_path, [repo], "incident-2", "session-2")
            entry = documents(repo / "docs/EXPERIENCE.md")[first["experience_id"]]
            self.assertEqual("confirmed", result["action"])
            self.assertEqual((2, 2), (entry["confidence"]["wins"], entry["confidence"]["independent_incidents"]))

    def test_same_incident_in_different_session_does_not_confirm(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_repo(directory, "project")
            global_path = Path(directory) / "global.md"
            first = record(repo, global_path, [repo], "incident-1", "session-1", "run-1")
            result = record(repo, global_path, [repo], "incident-1", "session-2", "run-2")
            entry = documents(repo / "docs/EXPERIENCE.md")[first["experience_id"]]
            self.assertEqual("duplicate_incident", result["action"])
            self.assertEqual("provisional", entry["status"])

    def test_deprecated_entry_preserves_original_and_is_addressable(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_repo(directory, "project")
            global_path = Path(directory) / "global.md"
            created = record(repo, global_path, [repo], "incident-1", "session-1")
            path = repo / "docs/EXPERIENCE.md"
            original = documents(path)[created["experience_id"]]
            experience.deprecate(path, created["experience_id"], "regression", {"run_id": "run-red"})
            changed = documents(path)[created["experience_id"]]
            self.assertEqual("deprecated", changed["status"])
            self.assertEqual(original["cue"], changed["cue"])
            self.assertEqual(original["lesson"], changed["lesson"])
            self.assertEqual("regression", changed["deprecated_reason"])

    def test_promotion_failure_rolls_every_file_back_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = make_repo(root, "A"), make_repo(root, "B")
            global_path = root / "global.md"
            record(a, global_path, [a], "incident-a1", "session-a1")
            record(a, global_path, [a], "incident-a2", "session-a2")
            before_a = (a / "docs/EXPERIENCE.md").read_bytes()
            original_write = experience._atomic_write
            calls = 0

            def fail_second(path, content):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected second write failure")
                return original_write(path, content)

            with mock.patch.object(experience, "_atomic_write", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "second write"):
                    record(b, global_path, [a, b], "incident-b1", "session-b1")
            self.assertEqual(before_a, (a / "docs/EXPERIENCE.md").read_bytes())
            self.assertFalse((b / "docs/EXPERIENCE.md").exists())
            self.assertFalse(global_path.exists())

    def test_symptom_transcript_never_enters_experience_file(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = make_repo(directory, "project")
            path = repo / "docs/EXPERIENCE.md"
            record(repo, Path(directory) / "global.md", [repo], "incident-1", "session-1",
                   secret="客戶電話 0912-000-000")
            self.assertNotIn("客戶電話", path.read_text(encoding="utf-8"))
            self.assertNotIn("0912", path.read_text(encoding="utf-8"))


class ExperienceBehaviorTests(unittest.TestCase):
    def test_cross_project_hint_then_real_win_promotes_once_with_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = make_repo(root, "A"), make_repo(root, "B")
            global_path = root / "global.md"
            record(a, global_path, [a], "incident-a1", "session-a1")
            confirmed = record(a, global_path, [a], "incident-a2", "session-a2")
            hints = experience.transplant_hints(b, [a, b])
            self.assertEqual(1, len(hints))
            self.assertFalse(global_path.exists())

            promoted = record(b, global_path, [a, b], "incident-b1", "session-b1")
            self.assertEqual("promoted", promoted["action"])
            global_entries = documents(global_path)
            self.assertEqual(1, len(global_entries))
            global_entry = next(iter(global_entries.values()))
            lineage = {(item["repo"], item["experience_id"]) for item in global_entry["lineage"]}
            self.assertIn((str(a.resolve()), confirmed["experience_id"]), lineage)
            self.assertIn((str(b.resolve()), promoted["experience_id"]), lineage)
            self.assertEqual({"incident-a1", "incident-a2", "incident-b1"}, {
                item["run_id"].removeprefix("run-") for item in global_entry["evidence"]
            })
            for repo in (a, b):
                text = (repo / "docs/EXPERIENCE.md").read_text(encoding="utf-8")
                self.assertIn("已升級全域", text)
                self.assertNotIn("A repeated guard failure", text)
                self.assertNotIn("root_cause_logic", text)

    def test_station_apply_success_records_experience_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root, "subject")
            run_dir = root / "run"
            (run_dir / "candidates/A").mkdir(parents=True)
            (run_dir / "tests").mkdir()
            (run_dir / "diagnoses").mkdir()
            (run_dir / "candidate-requirements.json").write_text(json.dumps({"requirements": [{
                "diagnosis_id": "diagnosis-inc-finding-1", "incident_id": "inc", "target": "subject",
                "finding": {"statement": finding("wire")["statement"], "angles": finding("wire")["angles"]},
                "machine_evidence_refs": finding("wire")["machine_evidence_refs"],
            }]}), encoding="utf-8")
            (run_dir / "diagnoses/inc.json").write_text(json.dumps({"diagnosis_id": "diagnosis-inc", "findings": [{
                **finding("wire"), "target": "subject",
            }]}), encoding="utf-8")
            (run_dir / "candidates/A/manifest.json").write_text(json.dumps({
                "diagnosis_ids": ["diagnosis-inc-finding-1"], "strategy_name": "root_cause_logic",
            }), encoding="utf-8")
            (run_dir / "tests/A.json").write_text(json.dumps({"commands": []}), encoding="utf-8")
            runner = station_host.PythonStationRunner.__new__(station_host.PythonStationRunner)
            runner.run_dir, runner.runs_root, runner.target_repo = run_dir, root / "runs", repo
            runner.host_root, runner.run_id = root, "run-wire"
            runner.ticket = {"session_id": "session-wire"}
            runner._winner_package = lambda *_args: {"state": "SCORED"}
            runner._dirty = lambda *_args: False
            runner._receipt_matches = lambda *_args, **_kwargs: True
            runner._apply_tests = lambda *_args: {"passed": True}
            runner._project_repos = lambda: [repo]
            with mock.patch.object(station_host.transaction, "run_transaction", return_value={
                "state": "CANARY_OK", "winner_commit": "b" * 40, "canary": {"passed": True},
            }), mock.patch.object(experience, "DEFAULT_GLOBAL_PATH", root / "global.md"):
                result = runner._apply("A")
            self.assertEqual("CANARY_OK", result["status"])
            recorded = json.loads((run_dir / "experience.json").read_text(encoding="utf-8"))
            self.assertEqual("provisional", recorded["action"])
            self.assertTrue((repo / "docs/EXPERIENCE.md").is_file())
            with mock.patch.object(experience, "record_win", side_effect=OSError("ledger unavailable")):
                runner._update_experience("A", {"status": "CANARY_OK", "commit_sha": "b" * 40})
            self.assertIn("ledger unavailable", json.loads((run_dir / "experience.json").read_text())["error"])

    def test_candidate_station_supplies_transplant_hints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root, "subject")
            run = root / "run"
            run.mkdir()
            (run / "candidate-requirements.json").write_text('{"requirements": []}', encoding="utf-8")
            runner = station_host.PythonStationRunner.__new__(station_host.PythonStationRunner)
            runner.allowed_paths, runner.target_repo, runner.run_dir = ["x.py"], repo, run
            runner.host_root, runner.runs_root, runner.run_id = ROOT, root / "runs", "run-1234abcd"
            runner._candidate = lambda request: request
            runner._project_repos = lambda: [repo]
            hints = [{"lesson_key": "key", "cue": "cue", "lesson": "lesson", "source_repo": "A", "experience_id": "exp"}]
            with mock.patch.object(experience, "transplant_hints", return_value=hints), \
                    mock.patch.object(station_host.candidate_adapter, "generate_candidates", return_value={"status": "CANDIDATES_DONE"}) as generate:
                runner.candidates({})
            self.assertEqual(hints, generate.call_args.kwargs["transplant_hints"])

    def test_candidate_request_contains_hints_as_advice_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root, "subject")
            run, evaluator = root / "run", root / "evaluator"
            run.mkdir()
            evaluator.mkdir(mode=0o500)
            calls = []
            hints = [{"lesson_key": "key", "cue": "cue", "lesson": "lesson",
                      "source_repo": "A", "experience_id": "exp"}]
            requirement = {"diagnosis_id": "d", "trusted_decision_refs": ["human"], "lite": True,
                           "target": "subject", "finding": {"statement": "fix it"}}

            def candidate(request):
                calls.append(request)
                Path(request["worktree"], "x.py").write_text(request["anonymous_id"] + "\n", encoding="utf-8")
                return {"diagnosis_ids": ["d"], "trusted_decision_refs": ["human"]}

            candidate_adapter.generate_candidates(
                repo, run, [requirement], ["x.py"], evaluator, candidate,
                {"catalog_version": 1, "seed": 1, "timeout_seconds": 1,
                 "max_changed_files": 3, "max_changed_lines": 200, "lite": True},
                transplant_hints=hints,
            )
            self.assertEqual([hints, hints, hints], [item["transplant_hints"] for item in calls])

    def test_station_revert_deprecates_an_existing_lesson(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = make_repo(root, "subject")
            created = record(repo, root / "global.md", [repo], "wire", "session-old")
            run = root / "run"
            (run / "candidates/A").mkdir(parents=True)
            (run / "diagnoses").mkdir()
            (run / "candidate-requirements.json").write_text(json.dumps({"requirements": [{
                "diagnosis_id": "diagnosis-inc-finding-1", "incident_id": "inc", "target": "subject",
                "finding": {"statement": finding("wire")["statement"]},
                "machine_evidence_refs": finding("wire")["machine_evidence_refs"],
            }]}), encoding="utf-8")
            (run / "diagnoses/inc.json").write_text(json.dumps({"findings": [finding("wire")]}), encoding="utf-8")
            (run / "candidates/A/manifest.json").write_text(json.dumps({
                "diagnosis_ids": ["diagnosis-inc-finding-1"], "strategy_name": "root_cause_logic",
            }), encoding="utf-8")
            runner = station_host.PythonStationRunner.__new__(station_host.PythonStationRunner)
            runner.run_dir, runner.target_repo, runner.run_id = run, repo, "run-revert"
            runner.ticket = {"session_id": "session-new"}
            runner._update_experience("A", {"status": "RESTORE_VERIFIED", "revert_sha": "c" * 40})
            entry = documents(repo / "docs/EXPERIENCE.md")[created["experience_id"]]
            self.assertEqual("deprecated", entry["status"])
            self.assertEqual("deprecated", json.loads((run / "experience.json").read_text())["action"])

    def test_only_experience_module_names_the_shared_file(self):
        offenders = []
        for path in (ROOT / "retro2").glob("*.py"):
            if path.name not in {"experience.py", "config.py"} and "EXPERIENCE.md" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
