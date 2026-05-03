"""Passive portal catalog.

A per-portal YAML file ( ``portals/<portal_id>/catalog.yaml`` ) that
accumulates structural facts about a portal — pages visited, buttons
seen, dropdowns and their option sets, form fields and types —
captured passively whenever the operator drives the portal during
teach mode (and eventually replay too).

The catalog is *additive*: every new ``page_snapshot`` event from the
grabber merges into the existing catalog, growing the union of
observed elements per page. Nothing is ever deleted — if a portal
removes a button, the catalog still records that we once saw it,
which is useful audit data ("this used to exist; did the portal
actually remove it, or is the recording stale?").

The catalog is consumed by:

  - The planner — for better clarify questions ("the layouts I've
    seen are X, Y, Z; pick one").
  - Future feasibility checks — "your goal mentions option Q but the
    catalog has only seen P, R, S in this dropdown; clarify."
  - Manual inspection — operators can read the YAML to understand
    what their pilot has observed about the portal.

Schema is intentionally permissive — pages a planner has never used
can be present without breaking anything.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


CATALOG_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CatalogButton(BaseModel):
    label: str = ""
    role: str = "button"
    test_id: str | None = None


class CatalogInput(BaseModel):
    name: str | None = None
    type: str = "text"
    label: str | None = None
    placeholder: str | None = None
    required: bool = False
    test_id: str | None = None


class CatalogSelectOption(BaseModel):
    value: str = ""
    text: str = ""


class CatalogSelect(BaseModel):
    name: str | None = None
    label: str | None = None
    test_id: str | None = None
    options: list[CatalogSelectOption] = Field(default_factory=list)


class CatalogLink(BaseModel):
    href: str = ""
    text: str = ""
    test_id: str | None = None


class CatalogPage(BaseModel):
    """One page in the portal. Aggregates observations across visits."""

    url: str
    """Last-observed full URL — kept for reference; the catalog key is
    the URL path so query strings don't fragment the catalog."""

    path: str = ""
    """URL path (no query). Used as the catalog key."""

    titles: list[str] = Field(default_factory=list)
    """All titles observed for this path. Multiple entries means the
    title varied across visits (often state-dependent)."""

    buttons: list[CatalogButton] = Field(default_factory=list)
    inputs: list[CatalogInput] = Field(default_factory=list)
    selects: list[CatalogSelect] = Field(default_factory=list)
    links: list[CatalogLink] = Field(default_factory=list)

    visit_count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None


class PortalCatalog(BaseModel):
    schema_version: int = CATALOG_SCHEMA_VERSION
    portal_id: str
    base_url: str = ""
    pages: dict[str, CatalogPage] = Field(default_factory=dict)
    """Keyed by URL path."""


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


def _normalize_path(url: str) -> str:
    """Extract the path component, drop query + fragment. Empty → '/'."""
    if not url:
        return "/"
    # Cheap parse — avoid urllib.parse to keep this dependency-light.
    s = url
    if "://" in s:
        s = s.split("://", 1)[1]
        if "/" in s:
            s = "/" + s.split("/", 1)[1]
        else:
            s = "/"
    if "?" in s:
        s = s.split("?", 1)[0]
    if "#" in s:
        s = s.split("#", 1)[0]
    return s or "/"


def _merge_buttons(
    existing: list[CatalogButton], incoming: list[dict[str, Any]]
) -> list[CatalogButton]:
    seen = {(b.label, b.role, b.test_id) for b in existing}
    out = list(existing)
    for raw in incoming:
        b = CatalogButton(
            label=raw.get("label") or "",
            role=raw.get("role") or "button",
            test_id=raw.get("testId"),
        )
        key = (b.label, b.role, b.test_id)
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out


def _merge_inputs(
    existing: list[CatalogInput], incoming: list[dict[str, Any]]
) -> list[CatalogInput]:
    seen = {(i.name, i.type, i.test_id) for i in existing}
    out = list(existing)
    for raw in incoming:
        ci = CatalogInput(
            name=raw.get("name"),
            type=raw.get("type") or "text",
            label=raw.get("label"),
            placeholder=raw.get("placeholder"),
            required=bool(raw.get("required", False)),
            test_id=raw.get("testId"),
        )
        key = (ci.name, ci.type, ci.test_id)
        if key not in seen:
            seen.add(key)
            out.append(ci)
    return out


def _merge_selects(
    existing: list[CatalogSelect], incoming: list[dict[str, Any]]
) -> list[CatalogSelect]:
    by_key: dict[tuple[str | None, str | None, str | None], CatalogSelect] = {
        (s.name, s.label, s.test_id): s for s in existing
    }
    for raw in incoming:
        key = (raw.get("name"), raw.get("label"), raw.get("testId"))
        opts_raw = raw.get("options") or []
        opts = [
            CatalogSelectOption(
                value=o.get("value") or "",
                text=o.get("text") or "",
            )
            for o in opts_raw
        ]
        if key in by_key:
            existing_opts = by_key[key].options
            existing_set = {(o.value, o.text) for o in existing_opts}
            for o in opts:
                if (o.value, o.text) not in existing_set:
                    existing_opts.append(o)
                    existing_set.add((o.value, o.text))
        else:
            by_key[key] = CatalogSelect(
                name=raw.get("name"),
                label=raw.get("label"),
                test_id=raw.get("testId"),
                options=opts,
            )
    return list(by_key.values())


def _merge_links(
    existing: list[CatalogLink], incoming: list[dict[str, Any]]
) -> list[CatalogLink]:
    seen = {(l.href, l.text, l.test_id) for l in existing}
    out = list(existing)
    for raw in incoming:
        l = CatalogLink(
            href=raw.get("href") or "",
            text=raw.get("text") or "",
            test_id=raw.get("testId"),
        )
        key = (l.href, l.text, l.test_id)
        if key not in seen:
            seen.add(key)
            out.append(l)
    return out


def merge_snapshot(
    catalog: PortalCatalog, snapshot: dict[str, Any]
) -> PortalCatalog:
    """Merge a single ``page_snapshot`` event into the catalog. Mutates
    + returns the catalog instance."""
    url = snapshot.get("page_url") or ""
    path = _normalize_path(url)
    title = snapshot.get("title") or ""
    now = datetime.utcnow()

    page = catalog.pages.get(path)
    if page is None:
        page = CatalogPage(
            url=url,
            path=path,
            titles=[title] if title else [],
            visit_count=1,
            first_seen=now,
            last_seen=now,
        )
        catalog.pages[path] = page
    else:
        page.url = url
        if title and title not in page.titles:
            page.titles.append(title)
        page.visit_count += 1
        page.last_seen = now

    page.buttons = _merge_buttons(page.buttons, snapshot.get("buttons") or [])
    page.inputs = _merge_inputs(page.inputs, snapshot.get("inputs") or [])
    page.selects = _merge_selects(page.selects, snapshot.get("selects") or [])
    page.links = _merge_links(page.links, snapshot.get("links") or [])
    return catalog


# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------


def catalog_path(portals_dir: Path, portal_id: str) -> Path:
    return portals_dir / portal_id / "catalog.yaml"


def load_catalog(
    portals_dir: Path, portal_id: str, base_url: str = ""
) -> PortalCatalog:
    """Load existing catalog or return a fresh empty one for this portal."""
    p = catalog_path(portals_dir, portal_id)
    if not p.exists():
        return PortalCatalog(portal_id=portal_id, base_url=base_url)
    try:
        import yaml

        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return PortalCatalog.model_validate(data)
    except Exception:
        # Corrupt catalog — start fresh rather than crash. Operator can
        # diff their git history if they care.
        return PortalCatalog(portal_id=portal_id, base_url=base_url)


def save_catalog(catalog: PortalCatalog, portals_dir: Path) -> Path:
    p = catalog_path(portals_dir, catalog.portal_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    import yaml

    p.write_text(
        yaml.safe_dump(
            catalog.model_dump(mode="json"),
            sort_keys=True,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return p


def render_for_prompt(catalog: PortalCatalog, *, max_chars: int = 4000) -> str:
    """Compact text form for inclusion in planner / clarify prompts.

    Names every observed page + lists each page's buttons (by label),
    selects (with their first-30 option texts), and required inputs.
    Truncated to ``max_chars`` to fit alongside the portal context.
    """
    lines: list[str] = [f"Portal catalog: {catalog.portal_id}"]
    if catalog.base_url:
        lines.append(f"Base URL: {catalog.base_url}")
    for path, page in sorted(catalog.pages.items()):
        title = page.titles[-1] if page.titles else ""
        lines.append(f"\nPage {path}  ({title}, visits={page.visit_count})")
        if page.buttons:
            labels = [b.label for b in page.buttons if b.label]
            if labels:
                lines.append(
                    "  buttons: " + ", ".join(sorted(set(labels))[:30])
                )
        if page.selects:
            for s in page.selects:
                option_texts = [o.text for o in s.options if o.text]
                lines.append(
                    f"  select [{s.label or s.name or s.test_id or '?'}]: "
                    + ", ".join(option_texts[:30])
                )
        required = [
            (i.label or i.name or i.test_id or "?")
            for i in page.inputs
            if i.required
        ]
        if required:
            lines.append("  required inputs: " + ", ".join(required))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text
