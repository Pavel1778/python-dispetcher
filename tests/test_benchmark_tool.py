"""The benchmark tooling: the comparison is pure, and the script parses.

The benchmark itself (``tools/benchmark.py``) starts a real server and database
and is run by hand; a test that ran it would make the suite slow for no extra
coverage. What *is* tested here is the part that is easy to get wrong and cheap
to check: the comparison of two runs, and that the script is importable (so a
syntax error or a bad import cannot reach a release unnoticed).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclasses (which resolve their annotations
    # through sys.modules) can be defined by the loaded module.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def test_the_benchmark_script_imports_and_exposes_its_entrypoint():
    module = _load("benchmark", os.path.join(ROOT, "tools", "benchmark.py"))
    assert callable(module.main)
    assert callable(module._percentiles)


def test_percentiles_of_an_empty_series_are_zero():
    module = _load("benchmark", os.path.join(ROOT, "tools", "benchmark.py"))
    assert module._percentiles([])["p50"] == 0.0


def test_percentiles_are_sorted_and_bounded():
    module = _load("benchmark", os.path.join(ROOT, "tools", "benchmark.py"))
    out = module._percentiles([5.0, 1.0, 3.0, 2.0, 4.0])
    assert out["min"] == 1.0 and out["max"] == 5.0
    assert out["p50"] == 3.0
    assert out["p90"] == 5.0


def test_the_compare_tool_reports_a_delta(tmp_path):
    before = {"measurements": {"x_ms": {"p50": 10.0, "p90": 12.0}}}
    after = {"measurements": {"x_ms": {"p50": 5.0, "p90": 6.0}}}
    b = tmp_path / "b.json"
    a = tmp_path / "a.json"
    b.write_text(json.dumps(before), encoding="utf-8")
    a.write_text(json.dumps(after), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "benchmark_compare.py"),
         str(b), str(a)],
        capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "x_ms" in done.stdout
    assert "-50.0%" in done.stdout, done.stdout


# ---------------------------------------------------------------- loadtest
def _loadtest():
    return _load("loadtest", os.path.join(ROOT, "tools", "loadtest.py"))


def test_the_loadtest_script_imports_and_exposes_its_entrypoint():
    module = _loadtest()
    assert callable(module.main)
    assert callable(module._percentiles)


def test_loadtest_percentiles_match_the_benchmark_convention():
    module = _loadtest()
    assert module._percentiles([])["p50"] == 0.0
    out = module._percentiles([5.0, 1.0, 3.0, 2.0, 4.0])
    assert out["min"] == 1.0 and out["max"] == 5.0
    assert out["p50"] == 3.0 and out["p90"] == 5.0
    # p95 is a loadtest addition over the benchmark's shape.
    assert out["p95"] == 5.0


def test_loadtest_weights_expand_to_a_literal_plan():
    """Each worker indexes a fixed list in a loop, so weights become repeats."""
    module = _loadtest()
    plan = [module.Call("GET", "/a", weight=2), module.Call("GET", "/b", weight=1)]
    expanded = module._expand(plan)
    assert expanded.count(plan[0]) == 20   # weight 2 -> 20 entries
    assert expanded.count(plan[1]) == 10   # weight 1 -> 10 entries


def test_loadtest_samples_are_recorded_per_call_and_errors_counted():
    module = _loadtest()
    samples = module.Samples()
    samples.record("GET /a", 0.001, ok=True)
    samples.record("GET /a", 0.002, ok=False)
    assert samples.total == 2
    assert samples.by_call["GET /a"] == [1.0, 2.0]
    assert samples.errors["GET /a"] == 1


def test_loadtest_mixes_cover_the_studio_reads_and_a_write():
    module = _loadtest()
    reads = module._mix("reads", "p", "c")
    writes = module._mix("writes", "p", "c")
    mixed = module._mix("mixed", "p", "c")
    read_paths = {c.path for c in reads}
    assert "/api/studio" in read_paths
    assert "/api/commands" in read_paths
    assert any(c.method == "POST" for c in writes)
    assert len(mixed) > len(reads)          # mixed is reads plus a write
