"""Structured JSON-Lines Event Telemetry Logger.

Emits every state transition, tool call, token increment, and evaluator check
to .harness/events.jsonl for complete auditable execution tracing.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class TelemetryLogger:
    """Appends structured JSON-Lines events to disk and optional callbacks."""

    def __init__(self, log_path: Optional[str] = None):
        self.log_path = log_path or os.path.join(".harness", "events.jsonl")
        Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event_type: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Emits a single telemetry event record with UTC timestamp."""
        payload: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
        }
        if data:
            payload.update(data)

        record_line = json.dumps(payload, default=str) + "\n"
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(record_line)

        return payload

    def timer(self):
        """Context manager helper for measuring duration in milliseconds."""
        return _TelemetryTimer(self)


class _TelemetryTimer:
    def __init__(self, logger: TelemetryLogger):
        self.logger = logger
        self.start_time: float = 0.0

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.start_time) * 1000)
