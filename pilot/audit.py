"""Audit logger — JSONL events plus before/after screenshots per action.

Everything that happens in a session is recorded here:
- task start / end events
- gate approvals / rejections
- errors
- free-form info entries

The output is fully inspectable after a run for debugging and compliance.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Page

from .models import AuditEvent


class AuditLogger:
    def __init__(self, session_id: str, base_dir: Path):
        self.session_id = session_id
        self.session_dir = base_dir / session_id
        self.screenshots_dir = self.session_dir / "screenshots"
        self.log_path = self.session_dir / "audit_log.jsonl"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        kind: str,
        message: str,
        task_id: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        event = AuditEvent(
            session_id=self.session_id,
            task_id=task_id,
            kind=kind,
            message=message,
            data=data,
        )
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")

    def screenshot(self, page: Page, label: str) -> str:
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_label = "".join(
            c if c.isalnum() or c in ("-", "_") else "_" for c in label
        )
        path = self.screenshots_dir / f"{ts}_{safe_label}.png"
        try:
            page.screenshot(path=str(path), full_page=False)
        except Exception as e:
            self.log("error", f"screenshot failed: {e}", data={"label": label})
            return ""
        return str(path)
