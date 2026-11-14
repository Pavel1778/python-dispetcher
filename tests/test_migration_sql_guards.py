"""Static guards over the migration SQL: no database required.

These run everywhere, including a checkout with no PostgreSQL. The
production startup failure was a single-word typo in one migration, and a
guard that needs a live server (or the Supabase roles) would have skipped
past it on the CI runner -- so the exact defect is asserted on the files
themselves.
"""
from __future__ import annotations

from pathlib import Path

def test_no_migration_uses_a_record_variable_in_format() -> None:
    """A migration that loops over roles must declare its loop variable ``text``.

    The production startup failure was a single-word typo: migration 0037
    declared the ``FOR r IN SELECT rolname ...`` loop variable as ``record``
    (every other migration declares ``text``). ``format('%I', <record>)``
    serialises the whole row to ``(authenticated)`` and then quotes it, so the
    statement targets a role literally named ``"(authenticated)"`` and the whole
    migration aborts. This guard needs no database, so it runs everywhere and
    fails on the exact defect regardless of whether the test server has the
    Supabase roles.
    """
    import re

    migrations = Path(__file__).resolve().parent.parent / "migrations"
    offenders: list[str] = []
    for path in sorted(migrations.glob("*.sql")):
        body = path.read_text(encoding="utf-8")
        # A ``format('%I', r)`` (or ``%s``/``%L``) whose argument is the bare
        # loop variable ``r`` is only correct when ``r`` is a text scalar.
        uses_bare_r = re.search(r"format\([^)]*%[ILs][^)]*,\s*r\s*\)", body)
        if not uses_bare_r:
            continue
        if re.search(r"DECLARE\s+r\s+record\b", body):
            offenders.append(path.name)
    assert offenders == [], (
        "these migrations declare the loop variable as `record` but pass it to "
        f"format('%I', r): {offenders}; declare it `text`")



