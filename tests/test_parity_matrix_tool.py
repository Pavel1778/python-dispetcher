"""The parity matrix tool must count real comparisons, and they must agree.

``tools/parity_matrix.py`` is the honest counter the mission asks for: it runs
the Rust binary and the Python authority on the same inputs and records one row
per *distinct* comparison. This test pins the gate - at least 100 meaningful
comparisons, zero disagreements - so a change that drops a component from parity
fails here rather than passing silently. It skips cleanly when the Rust
toolchain is absent, and says so.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import parity_matrix  # noqa: E402


def test_at_least_one_hundred_real_comparisons_agree():
    binary = parity_matrix._rust_bin()
    if binary is None:
        pytest.skip("no Rust toolchain (set NEXUS_RUST_BIN to a built nexus-rs)")
    matrix = parity_matrix.collect(binary)
    summary = matrix.summary()
    assert summary["disagree"] == 0, [
        r for r in matrix.rows if r["compared"] and r["agree"] is False]
    assert summary["compared"] >= 100, (
        f"only {summary['compared']} real comparisons; the mission requires >= 100"
    )
    # Every compared row must actually have an answer, not a placeholder.
    for row in matrix.rows:
        if row["compared"]:
            assert row["agree"] in (True, False), row


def test_areas_without_a_rust_counterpart_are_named_not_counted():
    binary = parity_matrix._rust_bin()
    if binary is None:
        pytest.skip("no Rust toolchain (set NEXUS_RUST_BIN to a built nexus-rs)")
    matrix = parity_matrix.collect(binary)
    notes = {r["area"]: r["note"] for r in matrix.rows if not r["compared"]}
    assert notes, "expected some NOT IMPLEMENTED / NOT COMPARABLE areas"
    assert all(n in ("NOT IMPLEMENTED", "NOT COMPARABLE") for n in notes.values())
