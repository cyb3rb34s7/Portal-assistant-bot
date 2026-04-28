"""Intake stage.

Extracts structured ``IntakeEntities`` from the operator's goal +
attachments. Strategy:

  1. Deterministic pre-pass.
       - CSV: parsed with stdlib ``csv``; columns matching common
         id/title patterns are mined.
       - Folder: enumerated; files are matched to ids by stem.
       - PPTX: best-effort text extraction using stdlib zipfile (the
         pptx is a zip of XML files). Saves us a python-pptx dep for
         v1; we only need text snippets, not formatting.
       - Plain text / markdown: read whole.
  2. Regex extraction of obvious tokens (asset ids ``A-NNNN``, ISO
     dates).
  3. LLM stage to refine: given the goal + a compact summary of what
     we extracted, propose a final ``IntakeEntities`` with role labels
     (which date is the schedule_start vs end, which file matches
     which asset, etc.).

The LLM is asked to return JSON matching ``IntakeEntities``. If it
fails twice, we fall back to the deterministic-only result rather than
raising — intake should be permissive; the planner stage can ask
clarify questions if the entities are insufficient.
"""

from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from pathlib import Path

from pydantic import BaseModel, Field

from pilot.agent.ai_client import AIClient, Message, complete_structured
from pilot.agent.ai_client.structured import StructuredOutputError
from pilot.agent.schemas.domain import (
    ContentItem,
    CsvAttachment,
    DateExtraction,
    FileResolution,
    IntakeEntities,
)
from pilot.agent.schemas.protocol import Attachment


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

_ASSET_ID_RE = re.compile(r"\bA-(\d{3,5})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_THUMB_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _read_pptx_text(path: Path, *, max_chars: int = 8000) -> str:
    """Best-effort extract of slide text from a .pptx without external deps.

    A pptx is a zip; slides are at ``ppt/slides/slideN.xml``. We pull
    every ``<a:t>...</a:t>`` text run and join them with newlines.
    """
    text_chunks: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            slide_names = sorted(
                n
                for n in zf.namelist()
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            )
            for name in slide_names:
                with zf.open(name) as f:
                    raw = f.read().decode("utf-8", errors="replace")
                # Naive but sufficient: pull <a:t>...</a:t> contents.
                for m in re.finditer(r"<a:t[^>]*>(.*?)</a:t>", raw, re.DOTALL):
                    txt = m.group(1).strip()
                    if txt:
                        text_chunks.append(txt)
                text_chunks.append("---")
    except (zipfile.BadZipFile, OSError):
        return ""
    joined = "\n".join(text_chunks)
    return joined[:max_chars]


def _read_csv_summary(path: Path, *, max_rows: int = 100) -> tuple[list[str], list[list[str]]]:
    """Return (header, sample_rows) for a CSV. Best-effort."""
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            rows = list(reader)
    except OSError:
        return [], []
    if not rows:
        return [], []
    header, *body = rows
    return header, body[:max_rows]


def _enumerate_folder(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    out: list[Path] = []
    for entry in sorted(path.iterdir()):
        if entry.is_file():
            out.append(entry)
    return out


def _read_text_file(path: Path, *, max_chars: int = 8000) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except OSError:
        return ""


def _extract_asset_ids(text: str) -> list[str]:
    seen: list[str] = []
    for m in _ASSET_ID_RE.finditer(text):
        sid = f"A-{m.group(1)}"
        if sid not in seen:
            seen.append(sid)
    return seen


def _extract_iso_dates(text: str) -> list[str]:
    seen: list[str] = []
    for m in _ISO_DATE_RE.finditer(text):
        d = m.group(1)
        if d not in seen:
            seen.append(d)
    return seen


# ---------------------------------------------------------------------------
# Pre-pass dataclass
# ---------------------------------------------------------------------------


class _PrepassFinding(BaseModel):
    asset_ids: list[str] = Field(default_factory=list)
    iso_dates: list[str] = Field(default_factory=list)
    csv_headers: list[str] = Field(default_factory=list)
    csv_sample_rows: list[list[str]] = Field(default_factory=list)
    csv_attachments: list[CsvAttachment] = Field(default_factory=list)
    folder_files: list[str] = Field(default_factory=list)
    pptx_text: str = ""
    plain_text: str = ""
    warnings: list[str] = Field(default_factory=list)


def _prepass(attachments: list[Attachment]) -> _PrepassFinding:
    f = _PrepassFinding()
    text_blob = ""

    for att in attachments:
        path = Path(att.path)
        kind = (att.kind or path.suffix.lstrip(".")).lower()

        if not path.exists():
            f.warnings.append(f"attachment not found: {att.path}")
            continue

        if kind in ("pptx",) or path.suffix.lower() == ".pptx":
            text = _read_pptx_text(path)
            f.pptx_text += "\n" + text
            text_blob += "\n" + text
        elif kind in ("csv",) or path.suffix.lower() == ".csv":
            header, rows = _read_csv_summary(path)
            f.csv_headers.extend(header)
            f.csv_sample_rows.extend(rows)
            row_dicts = [
                {h: (r[i] if i < len(r) else "") for i, h in enumerate(header)}
                for r in rows
            ]
            f.csv_attachments.append(
                CsvAttachment(path=str(path), headers=list(header), rows=row_dicts)
            )
            text_blob += "\n" + ",".join(header)
            for row in rows:
                text_blob += "\n" + ",".join(row)
        elif kind in ("folder",) or path.is_dir():
            for child in _enumerate_folder(path):
                f.folder_files.append(str(child))
        elif kind in ("txt", "md") or path.suffix.lower() in (".txt", ".md"):
            t = _read_text_file(path)
            f.plain_text += "\n" + t
            text_blob += "\n" + t
        else:
            f.warnings.append(
                f"attachment kind {kind!r} not specifically handled; "
                "skipped (will not contribute to entities)."
            )

    f.asset_ids = _extract_asset_ids(text_blob)
    f.iso_dates = _extract_iso_dates(text_blob)
    return f


def _match_thumbnails(asset_ids: list[str], folder_files: list[str]) -> list[FileResolution]:
    out: list[FileResolution] = []
    by_stem: dict[str, str] = {}
    for fp in folder_files:
        p = Path(fp)
        if p.suffix.lower() in _THUMB_EXTS:
            by_stem[p.stem.upper()] = fp
    for aid in asset_ids:
        match = by_stem.get(aid.upper())
        out.append(
            FileResolution(
                path=match or "",
                matched_to=aid if match else None,
                matched_kind="thumbnail" if match else None,
            )
        )
    # Folder files with no asset match are still surfaced for traceability
    matched_paths = {fr.path for fr in out if fr.path}
    for fp in folder_files:
        if fp not in matched_paths:
            out.append(FileResolution(path=fp, matched_to=None, matched_kind=None))
    return out


def _build_baseline_entities(prepass: _PrepassFinding) -> IntakeEntities:
    # Index CSV rows by their content_id-shaped column (any column whose
    # values look like A-NNNN). This lets us enrich ContentItems with
    # title / image_path / release_date / category / etc. without the
    # planner having to dig through raw_text_excerpts.
    csv_by_id: dict[str, dict[str, str]] = {}
    for csv_att in prepass.csv_attachments:
        id_col = None
        for h in csv_att.headers:
            if h.lower() in ("content_id", "id", "asset_id"):
                id_col = h
                break
        if id_col is None:
            continue
        for row in csv_att.rows:
            cid = row.get(id_col, "").strip()
            if cid and cid not in csv_by_id:
                csv_by_id[cid] = row

    items: list[ContentItem] = []
    for aid in prepass.asset_ids:
        csv_row = csv_by_id.get(aid, {})
        items.append(
            ContentItem(
                id=aid,
                title=csv_row.get("title") or None,
                thumbnail_path=csv_row.get("image_path") or None,
                raw_source="attachment-prepass",
                extra={
                    k: v for k, v in csv_row.items()
                    if k not in ("content_id", "id", "asset_id", "title", "image_path")
                    and v
                },
            )
        )
    files = _match_thumbnails(prepass.asset_ids, prepass.folder_files)

    # Tag matched thumbnails back onto items
    by_id = {item.id: item for item in items}
    for fr in files:
        if fr.matched_to and fr.matched_kind == "thumbnail":
            target = by_id.get(fr.matched_to)
            if target:
                target.thumbnail_path = fr.path

    dates = [DateExtraction(iso_date=d, role=None, raw_source="attachment-prepass") for d in prepass.iso_dates]

    warnings = list(prepass.warnings)
    missing = [it.id for it in items if it.thumbnail_path is None]
    if missing and prepass.folder_files:
        warnings.append(
            f"{len(missing)} asset(s) had no matching thumbnail file: "
            + ", ".join(missing[:5])
            + (" ..." if len(missing) > 5 else "")
        )

    raw_excerpts: list[str] = []
    if prepass.pptx_text:
        raw_excerpts.append(prepass.pptx_text[:600])
    if prepass.plain_text:
        raw_excerpts.append(prepass.plain_text[:600])
    if prepass.csv_headers:
        raw_excerpts.append(
            "CSV headers: " + ", ".join(prepass.csv_headers)
        )

    return IntakeEntities(
        content_items=items,
        dates=dates,
        files_resolved=files,
        csv_attachments=prepass.csv_attachments,
        raw_text_excerpts=raw_excerpts,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# LLM refinement
# ---------------------------------------------------------------------------


_INTAKE_SYSTEM_PROMPT = """\
You are the intake stage of a portal-automation assistant. Given the
operator's goal and a structured pre-pass of their attached files, you
must produce a clean IntakeEntities JSON object.

Rules:
- Do not invent asset ids that are not present in the pre-pass.
- Tag dates with a 'role' if the goal makes their meaning clear
  (schedule_start, schedule_end, go_live, deadline). Otherwise leave
  role null.
- If the operator's goal mentions thumbnails / images and the file
  resolutions show unmatched assets, list each in `warnings`.
- Be conservative with `raw_text_excerpts`: keep at most three short
  snippets that ground your decisions.
- Echo all warnings produced by the pre-pass.
"""


def _user_prompt(goal: str, prepass: _PrepassFinding, baseline: IntakeEntities) -> str:
    return f"""\
Operator goal:
{goal}

Pre-pass findings:
- asset ids found: {', '.join(prepass.asset_ids) or '(none)'}
- iso dates found: {', '.join(prepass.iso_dates) or '(none)'}
- folder files: {len(prepass.folder_files)} entries
- csv headers: {', '.join(prepass.csv_headers) or '(none)'}
- pre-pass warnings: {prepass.warnings or '(none)'}

Deterministic baseline (you may refine this):
{baseline.model_dump_json(indent=2)}

Return a JSON object validating against IntakeEntities. Keep
content_items in pre-pass order.
"""


async def run_intake(
    *,
    client: AIClient,
    goal: str,
    attachments: list[Attachment],
    use_llm: bool = True,
    model: str | None = None,
) -> IntakeEntities:
    """Top-level intake entry point.

    Args:
        client: AIClient for the optional refinement step.
        goal: operator's natural-language goal.
        attachments: file/folder attachments declared by the host.
        use_llm: when False, return the deterministic baseline only.
        model: override the AIClient's default model.

    Returns:
        IntakeEntities. Always returns; never raises for LLM trouble
        (falls back to the baseline silently and adds a warning).
    """
    prepass = _prepass(attachments)
    baseline = _build_baseline_entities(prepass)

    if not use_llm:
        return baseline

    try:
        refined = await complete_structured(
            client,
            messages=[
                Message(role="system", content=_INTAKE_SYSTEM_PROMPT),
                Message(role="user", content=_user_prompt(goal, prepass, baseline)),
            ],
            response_model=IntakeEntities,
            model=model,
            temperature=0.0,
            max_retries=2,
        )
    except StructuredOutputError as e:
        baseline.warnings.append(
            f"intake LLM refinement failed; using deterministic baseline ({e})"
        )
        return baseline

    # Defensive: if the LLM dropped warnings, re-merge them.
    seen_warnings = set(refined.warnings)
    for w in baseline.warnings:
        if w not in seen_warnings:
            refined.warnings.append(w)
    return refined
