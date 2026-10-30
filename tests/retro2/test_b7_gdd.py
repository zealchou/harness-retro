import ast
import importlib.util
import inspect
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = Path(os.environ.get("RETRO2_B7_MODULE", ROOT / "retro2/candidate_adapter.py"))


def load_module():
    spec = importlib.util.spec_from_file_location("retro2_b7_boundary", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScoreApplyBoundaryTests(unittest.TestCase):
    def test_candidate_layer_cannot_import_or_invoke_apply(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imports = []
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.append(node.func.attr)
        self.assertFalse(any("apply" in name.lower() for name in imports), imports)
        self.assertFalse(any(name.lower().startswith("apply") for name in calls), calls)
        self.assertNotIn("apply_runner", inspect.signature(load_module().generate_candidates).parameters)

    def test_apply_boundary_requires_scored_winner_package(self):
        transaction_path = ROOT / "retro2/apply/transaction.py"
        spec = importlib.util.spec_from_file_location("retro2_b8_boundary", transaction_path)
        transaction = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(transaction)
        with self.assertRaisesRegex(Exception, "schema v1 in SCORED state"):
            transaction._validate_package({"winner": {"patch": "raw candidate"}}, ROOT, ROOT)


if __name__ == "__main__":
    unittest.main()

