import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "retro2"))

import filter_adapter
import host_exits
import rejection_index
import station_host
from tests.retro2.test_r2lite import LiteFixture
from tests.retro2.test_r2lite import load_controller


def init_repo(root, name="subject"):
    repo = Path(root) / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "target.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    return repo


def supports_bwrap_namespace():
    if not shutil.which("bwrap") or not os.access(Path.home(), os.W_OK):
        return False
    return subprocess.run(["bwrap", "--ro-bind", "/", "/", "--unshare-net", "--", "true"], capture_output=True).returncode == 0


class RoundTwoAuditFixTests(unittest.TestCase):
    @unittest.skipUnless(supports_bwrap_namespace(), "requires writable HOME and a usable bwrap network namespace")
    def test_apply_test_child_starts_but_cannot_write_subject_or_git(self):
        with tempfile.TemporaryDirectory() as directory:
            subject = init_repo(directory)
            scratch = Path(directory) / "discardable"
            scratch.mkdir()
            script = (
                "import pathlib,sys; print('APPLY_CHILD_STARTED'); "
                "targets=[pathlib.Path(x) for x in sys.argv[1:]]; "
                "out=[]; "
                "\nfor p in targets:\n"
                " try: p.write_text('mutated'); out.append('WROTE')\n"
                " except OSError: out.append('DENIED')\n"
                "print(','.join(out))"
            )
            result = host_exits.run_apply_tests(
                [[sys.executable, "-B", "-c", script, str(subject / "target.txt"), str(subject / ".git" / "HEAD")]],
                subject, scratch, allow_network=True,
            )
            record = result["results"][0]
            self.assertIn("APPLY_CHILD_STARTED", record["stdout"])
            self.assertIn("DENIED,DENIED", record["stdout"])
            self.assertEqual("before\n", (subject / "target.txt").read_text(encoding="utf-8"))

    @unittest.skipUnless(supports_bwrap_namespace(), "requires writable HOME and a usable bwrap network namespace")
    def test_restricted_test_child_has_no_network_and_starts_before_write_denial(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
            root = Path(directory)
            writable = root / "worktree"
            writable.mkdir()
            protected = root / "protected.txt"
            protected.write_text("safe\n", encoding="utf-8")
            script = (
                "import pathlib,socket,sys; print('SANDBOX_STARTED'); "
                "p=pathlib.Path(sys.argv[1]); "
                "print('ABSENT' if not p.exists() else 'PRESENT'); "
                "\ntry:\n"
                "    if not p.exists():\n"
                "        raise OSError('not mounted')\n"
                "    p.write_text('bad'); print('WRITE_OK')\n"
                "except OSError: print('WRITE_DENIED')\n"
                "\ntry: socket.create_connection(('1.1.1.1', 53), .1); print('NETWORK_OK')\n"
                "except OSError: print('NETWORK_DENIED')"
            )
            subject = root / "subject"
            subject.mkdir()
            result = host_exits.run_allowlisted(
                [sys.executable, "-B", "-c", script, str(protected)], writable,
                cwd=subject, profile="apply", subject_root=subject, allow_network=False,
                env={"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin"), "LANG": "C"},
            )
            # Some locked-down CI kernels prohibit creating a network namespace.
            # That is fail-closed (the child never starts); supported kernels must
            # prove both the sentinel and denied socket.
            if result.returncode:
                self.assertIn("NETLINK_ROUTE", result.stderr)
            else:
                self.assertIn("SANDBOX_STARTED", result.stdout)
                self.assertIn("WRITE_DENIED", result.stdout)
                self.assertIn("NETWORK_DENIED", result.stdout)
            self.assertEqual("safe\n", protected.read_text(encoding="utf-8"))

    def test_security_approval_keeps_real_technical_blockers(self):
        def runner(request):
            name = request["skill_name"]
            if name == "security-audit-skill":
                return {"architecture": "a", "report": "r", "details": "d", "findings": {"findings": []}}
            blockers = ["wiring broken"] if name == "gunpla-audit" else []
            return {"markdown": "ok", "json": {"findings": [], "blockers": blockers}}

        candidate = {"candidate_id": "A", "patch": "+requests.get('https://example.invalid')\n", "changed_paths": ["docs/a.md"]}
        with tempfile.TemporaryDirectory() as directory:
            verdict = filter_adapter.filter_candidate(candidate, directory, runner, 20)
        self.assertEqual("REQUIRES_HUMAN_SECURITY_APPROVAL", verdict["status"])
        self.assertEqual(["gunpla:wiring broken"], verdict["technical_blockers"])

    def test_only_exact_approval_message_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiteFixture(directory)
            runner = fixture.runner()
            report = runner.run_dir / "report/report.md"
            report.parent.mkdir(parents=True)
            report.write_text("report\n", encoding="utf-8")
            os.utime(report, ns=(10_000_000_000, 10_000_000_000))
            for index, content in enumerate(("不要套 A", "我說『套 A』", "如果可以就套 A", "套用 A", " 套 A ")):
                ref = f"m-{index}"
                fixture.transcript.write_text(json.dumps({"type": "user", "uuid": ref, "origin": {"kind": "human"}, "timestamp_ns": 20_000_000_000, "message": {"content": content}}) + "\n", encoding="utf-8")
                self.assertEqual(index >= 3, runner._trusted_approval(ref, "A"), content)

    def test_declined_candidate_never_reaches_apply(self):
        # Scenario: 我回「不要套 A」不會被當成同意。
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiteFixture(directory)
            runner = fixture.runner()
            fixture.candidate(runner, patch="-one\n+one\ntwo\n")
            fixture.transcript.write_text(json.dumps({
                "type": "user", "uuid": "decline", "origin": {"kind": "human"},
                "message": {"content": "不要套 A"},
            }) + "\n", encoding="utf-8")
            with mock.patch.object(runner, "_semantic_review", return_value={"status": "FILTERED_OK"}), \
                    mock.patch.object(runner, "_apply") as apply:
                result = runner.apply_or_report({"candidate": "A", "approval_ref": "decline"})
            self.assertEqual(("REQUIRES_HUMAN_APPROVAL", "approval_not_human_or_candidate_mismatch"),
                             (result["status"], result["reason"]))
            apply.assert_not_called()

    def test_receipt_binds_actual_postimage_and_test_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiteFixture(directory)
            runner = fixture.runner()
            fixture.candidate(runner, content="after\n")
            command = [[sys.executable, "-B", "-m", "unittest", "tests.retro2.test_target"]]
            (runner.run_dir / "tests/A.json").write_text(json.dumps({"passed": True, "commands": command}), encoding="utf-8")
            runner._write_receipt("A", json.loads((runner.run_dir / "candidates/A/manifest.json").read_text()), {"commands": command, "passed": True})
            receipt_path = next((runner.run_dir / "receipts").glob("*.json"))
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(hashlib.sha256(b"after\n").hexdigest(), receipt["postimages"]["target.py"])
            self.assertTrue(runner._receipt_matches("A", approval_ref=None))
            for label, mutate, approval in (
                ("patch", lambda: (runner.run_dir / "candidates/A/candidate.patch").write_text("other", encoding="utf-8"), None),
                ("preimage", lambda: (fixture.repo / "target.py").write_text("changed\n", encoding="utf-8"), None),
                ("test contents", lambda: (fixture.repo / "tests/retro2/test_target.py").write_text("changed\n", encoding="utf-8"), None),
                ("approval", lambda: None, "approval"),
            ):
                with self.subTest(label=label):
                    mutate()
                    self.assertFalse(runner._receipt_matches("A", approval_ref=approval))
                # Restore the fixture and a fresh host receipt before the next mutation.
                (runner.run_dir / "candidates/A/candidate.patch").write_text("-one\n-two\n+one\n", encoding="utf-8")
                (fixture.repo / "target.py").write_text("one\ntwo\n", encoding="utf-8")
                (fixture.repo / "tests/retro2/test_target.py").write_text(
                    "import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
                runner._write_receipt("A", json.loads((runner.run_dir / "candidates/A/manifest.json").read_text()), {"commands": command, "passed": True})

    def test_rejection_key_requires_repo_preimage_and_patch_and_only_test_failures(self):
        incidents = [{"source_ref": "z", "content": "incident"}]
        first = rejection_index.make_key(incidents, "/repo-a", "head", {"a.py": "a" * 64}, "b" * 64, "policy", "tests")
        self.assertNotEqual(first, rejection_index.make_key(incidents, "/repo-b", "head", {"a.py": "a" * 64}, "b" * 64, "policy", "tests"))
        self.assertNotEqual(first, rejection_index.make_key(incidents, "/repo-a", "head", {"a.py": "c" * 64}, "b" * 64, "policy", "tests"))
        self.assertNotEqual(first, rejection_index.make_key(incidents, "/repo-a", "head", {"a.py": "a" * 64}, "d" * 64, "policy", "tests"))
        self.assertFalse(rejection_index.stable_reason("undiagnosed"))
        self.assertFalse(rejection_index.stable_reason("path_allowlist"))
        self.assertTrue(rejection_index.stable_reason("test_failed"))

    def test_same_patch_failure_skips_retest_but_new_patch_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LiteFixture(directory)
            runner = fixture.runner()
            fixture.candidate(runner, patch="-one\n+broken\n")
            manifest = json.loads((runner.run_dir / "candidates/A/manifest.json").read_text())
            key = runner._rejection_key(manifest)
            rejection_index.append(runner.runs_root / "run-prior", key, "test_failed", "stable", fixture.repo)
            with mock.patch.object(runner, "_run_tests", return_value={"passed": False, "commands": [], "results": []}) as tested:
                result = runner.test_gate({})
            self.assertEqual(("NO_CHANGE", "tests_failed"), (result["status"], result["reason"]))
            tested.assert_not_called()
            fixture.candidate(runner, patch="-one\n+different\n")
            with mock.patch.object(runner, "_run_tests", return_value={"passed": True, "commands": [], "results": []}) as tested:
                runner.test_gate({})
            tested.assert_called_once()

    def test_external_subject_formal_controller_path_reports_coverage_unknown(self):
        # Scenario: 在另一個專案觸發 retro，能跑完並出報告。
        with tempfile.TemporaryDirectory() as directory:
            subject = init_repo(directory)
            transcript = Path(directory) / "session.jsonl"
            transcript.write_text(json.dumps({"type": "user", "uuid": "maintainer", "origin": {"kind": "human"}, "message": {"content": "修正"}}) + "\n")
            ticket = {"schema_version": 1, "idempotency_key": "external-report", "controller_entry": "retro2/controller/run.py", "repo_root": str(subject), "transcript_path": str(transcript)}
            runner = station_host.PythonStationRunner(ticket, Path(directory) / "runs", command=lambda *_a, **_k: (0, "", ""), claude="claude")
            self.assertEqual([], runner._test_commands(["target.txt"]))
            result = runner._run_tests(subject, [])
            self.assertTrue(result["coverage_unknown"])

    def test_external_project_controller_execute_writes_a_report(self):
        # Scenario: 在另一個專案觸發 retro，能跑完並出報告。
        with tempfile.TemporaryDirectory() as directory:
            subject = init_repo(directory)
            ticket = Path(directory) / "ticket.json"
            ticket.write_text(json.dumps({
                "schema_version": 1, "idempotency_key": "external-controller", "controller_entry": "retro2/controller/run.py",
                "repo_root": str(subject),
            }), encoding="utf-8")
            statuses = iter(("HEALTHY", "EVIDENCE_OK", "DIAGNOSED", "CANDIDATES_DONE", "TESTS_PASSED", "FILTERED_OK", "REQUIRES_HUMAN_APPROVAL"))
            result = load_controller("round2_external_controller").execute_ticket(
                ticket, Path(directory) / "runs", lambda _station, _request: {"status": next(statuses), "reason": "coverage_unknown"},
            )
            self.assertEqual("REQUIRES_HUMAN_APPROVAL", result["status"])
            self.assertTrue(Path(result["report_md"]).is_file())


if __name__ == "__main__":
    unittest.main()
