"""Regression tests for the two findings on pull request #3.

Run from the repository root: python -m unittest discover -s regression_tests
"""
import ast
import importlib.util
import sqlite3
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("canflow_store", ROOT / "canflow/store.py")
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


def measurement_method():
    # Compile the actual UI method without importing Qt, so this regression also
    # runs in lightweight environments. Its database lookup remains real.
    module = ast.parse((ROOT / "canflow/app.py").read_text(encoding="utf-8"))
    cls = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name in ("AnalysisWindow", "MainWindow"))
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_measure_at")
    tree = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    namespace = {"nearest_sample": store.nearest_sample, "sample_clock_text": lambda timestamp, *args: str(timestamp)}
    exec(compile(tree, str(ROOT / "canflow/app.py"), "exec"), namespace)
    return namespace["_measure_at"]


class Label:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = text

    def setToolTip(self, text):
        pass


class MeasurementRegression(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE samples (signal TEXT, timestamp REAL, value REAL)")
        self.markers = []
        self.window = SimpleNamespace(
            db=self.db, files=[SimpleNamespace(first=1000.0)], measure_a=None,
            measure_b=None, measure_key=None, measure_label=Label(),
            plot=SimpleNamespace(viewRange=lambda: [[0.0, 10.0], [0, 1]]),
            time_source_combo=SimpleNamespace(currentData=lambda: "utc"),
        )
        # SignalKey is hashable in the application.
        class Key:
            name = "speed"
            def storage_key(self):
                return "speed"
        key = Key()
        self.window.chart = SimpleNamespace(
            measurement_enabled=True, focused=key, layers={key: SimpleNamespace(visible=True, unit="")},
            set_measure_markers=lambda a, b: self.markers.append((a, b)),
        )

    def tearDown(self):
        self.db.close()

    def test_click_snaps_to_visible_sample_when_closer_sample_is_outside(self):
        self.db.executemany("INSERT INTO samples VALUES (?, ?, ?)", [
            ("speed", 999.9, 1.0), ("speed", 1001.0, 2.0),
        ])
        measurement_method()(self.window, 0.0)
        self.assertEqual(self.window.measure_a, (1001.0, 2.0))
        self.assertEqual(self.markers, [(1.0, None)])

    def test_empty_viewport_reports_no_visible_sample(self):
        self.db.execute("INSERT INTO samples VALUES (?, ?, ?)", ("speed", 999.9, 1.0))
        measurement_method()(self.window, 0.0)
        self.assertIsNone(self.window.measure_a)
        self.assertIn("当前视野内暂无采样点", self.window.measure_label.text)

    def test_visible_interval_includes_both_boundaries(self):
        self.db.executemany("INSERT INTO samples VALUES (?, ?, ?)", [
            ("speed", 1000.0, 1.0), ("speed", 1010.0, 2.0),
        ])
        for target, expected in [(999.0, (1000.0, 1.0)), (1011.0, (1010.0, 2.0))]:
            with self.subTest(target=target):
                self.assertEqual(store.nearest_sample(self.db, "speed", target, start=1000.0, end=1010.0), expected)


class SiteRegression(unittest.TestCase):
    def test_site_is_tracked_as_regular_files_and_submodule_commands_work(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=ROOT, check=True, text=True, capture_output=True).stdout

        entries = git("ls-files", "--stage", "--", "site").splitlines()
        self.assertTrue(entries, "site must have tracked files")
        self.assertTrue(any(line.endswith("\tsite/dist/index.html") for line in entries))
        self.assertTrue(all(not line.startswith("160000 ") for line in entries), entries)
        git("submodule", "status")
        git("submodule", "update", "--init")


if __name__ == "__main__":
    unittest.main()
