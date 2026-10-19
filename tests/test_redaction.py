"""Secrets must never appear in logs."""

from __future__ import annotations

import json
import logging

from app.logging_config import (
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    scrub_mapping,
    scrub_text,
)

SECRET = "sk-super-secret-value-1234567890"
BRIDGE = "bridge-token-abcdef"


def test_scrub_text_removes_known_secret():
    text = f"calling api with {SECRET} now"
    assert SECRET not in scrub_text(text, [SECRET])
    assert "<redacted>" in scrub_text(text, [SECRET])


def test_scrub_text_removes_bearer_tokens():
    text = "Authorization: Bearer abcdef123456"
    out = scrub_text(text)
    assert "abcdef123456" not in out


def test_scrub_text_removes_sk_tokens():
    out = scrub_text(f"token={SECRET}")
    assert SECRET not in out


def test_scrub_mapping_redacts_sensitive_keys():
    data = {
        "id": "task-1",
        "authorization": "Bearer xyz",
        "sandbox_api_key": "leak-me",
        "nested": {"session_api_key": "leak-too", "ok": "fine"},
    }
    out = scrub_mapping(data, [SECRET])
    assert out["id"] == "task-1"
    assert out["authorization"] == "<redacted>"
    assert out["sandbox_api_key"] == "<redacted>"
    assert out["nested"]["session_api_key"] == "<redacted>"
    assert out["nested"]["ok"] == "fine"


def test_json_formatter_output_contains_no_secret():
    redaction = RedactionFilter([SECRET, BRIDGE])
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f"connecting with {SECRET} and {BRIDGE}",
        args=(),
        exc_info=None,
    )
    redaction.filter(record)
    rendered = formatter.format(record)
    assert SECRET not in rendered
    assert BRIDGE not in rendered
    payload = json.loads(rendered)
    assert payload["level"] == "INFO"


def test_configure_logging_installs_redaction(capsys):
    configure_logging("INFO", [SECRET])
    logger = logging.getLogger("redaction-test")
    logger.info("using key %s", SECRET)
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert "<redacted>" in captured.out


def test_json_formatter_scrubs_safe_extra_fields():
    """D8: secrets passed as ``extra_fields`` must be scrubbed too.

    ``safe_extra`` fields bypass ``record.msg``, so without an explicit scrub a
    secret handed to a log call as a field would be serialised verbatim.
    """
    redaction = RedactionFilter([SECRET, BRIDGE])
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="task failed",
        args=(),
        exc_info=None,
    )
    record.extra_fields = {
        "task_id": "task-1",
        "authorization": f"Bearer {BRIDGE}",
        "detail": f"upstream echoed {SECRET}",
        "nested": {"sandbox_api_key": SECRET},
    }
    redaction.filter(record)
    rendered = formatter.format(record)
    assert SECRET not in rendered
    assert BRIDGE not in rendered
    payload = json.loads(rendered)
    assert payload["task_id"] == "task-1"
    assert payload["nested"]["sandbox_api_key"] == "<redacted>"


def test_extra_fields_scrub_does_not_break_plain_records():
    redaction = RedactionFilter([SECRET])
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="nothing sensitive",
        args=(),
        exc_info=None,
    )
    assert redaction.filter(record) is True
    assert JsonFormatter().format(record)
