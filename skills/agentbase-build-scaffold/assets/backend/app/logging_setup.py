"""Logging: one JSON object per line on stdout in a deployed environment, readable text locally.

    {"ts": "2026-10-29T08:14:03.512Z", "level": "INFO", "logger": "app.service", "msg": "request",
     "request_id": "…", "trace_id": "…", "type": "chat", "status": 200, "duration_ms": 1840, …}

`request_id` (the platform's) and `trace_id` (Langfuse) are added to EVERY line written during a request, so a log line
and the trace of the same turn are one search apart; `msg` and exception text go through the same masking as the traces
(secrets, bearer tokens, URL credentials, e-mails, phone / ID numbers). Pass structured fields with
`log.info("name", extra={"fields": {"key": value}})`. Never log a message, an answer or a tool argument.
LOG_FORMAT: auto (json outside APP_ENV=local) | json | text. The SDK's own server (uvicorn) keeps its format.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from greennode_agentbase import GreenNodeAgentBaseContext

from app.config import Settings
from app.observability import tracing


def _ids() -> dict[str, str]:
    """request_id / trace_id of the request being served — never raises (a log call must not break a request)."""
    out: dict[str, str] = {}
    try:
        if request_id := GreenNodeAgentBaseContext.get_request_id():
            out["request_id"] = str(request_id)
        if trace_id := tracing.current_trace_id():
            out["trace_id"] = trace_id
    except Exception:  # noqa: BLE001
        pass
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": tracing.mask_text(record.getMessage()),
            **_ids(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            entry["exc"] = tracing.mask_text(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extra = {**_ids(), **getattr(record, "fields", {})}
        return line + "".join(f" {k}={v}" for k, v in extra.items()) if extra else line


def configure_logging(settings: Settings) -> None:
    use_json = settings.log_format == "json" or (
        settings.log_format == "auto" and not settings.is_local
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter()
        if use_json
        else TextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    # Replace a previous call's handler and a plain basicConfig() one — never another library's (pytest's capture)
    root.handlers[:] = [h for h in root.handlers if type(h) is not logging.StreamHandler]
    root.addHandler(handler)
    root.setLevel(settings.log_level)
