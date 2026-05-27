"""WI-05: Typed parameter codecs + constraint validation.

The runner resolves a step's param value through ``resolve_param``
BEFORE invoking the per-action handler. Each (type, codec) pair has a
deterministic, side-effect-free conversion to the canonical string the
page expects, followed by constraint validation. Failures raise
``ParamValidationError`` which the runner converts to
``ToolResult.error_kind="param_validation_failed"`` -- the page is not
touched.

Design notes:
  - Codecs are pure functions: no I/O except ``file_ref`` (filesystem
    existence check). This keeps validation cheap and unit-testable.
  - The codec output is always a single ``str`` (the page-side value)
    or ``list[str]`` (for string_list / object). The runner doesn't
    care about typed intermediates; it just wants the canonical wire
    representation.
  - Unknown codec values default to ``raw`` rather than failing -- the
    schema's Literal already restricts what makes it here, and a
    forward-compatible enum extension shouldn't break old runners.

See ``pilot.skill_models.SkillParam`` for the field definitions.
"""

from __future__ import annotations

import datetime as _dt
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Optional, Union

from .skill_models import OptionSnapshot, ParamConstraints, SkillParam


class ParamValidationError(Exception):
    """Raised when a param value fails codec resolution or constraint
    check. Carries the param name + a structured ``details`` dict so the
    runner can emit ``ToolResult.error_details``.

    Kept distinct from generic Exception so the runner's catch site can
    distinguish 'operator passed a bad date' from 'random Python error.'
    """

    def __init__(
        self,
        param_name: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(f"param {param_name!r}: {message}")
        self.param_name = param_name
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------


_ISO_DATE_INPUT_FORMATS: tuple[str, ...] = (
    # Common input formats accepted by codec=iso_date. Order matters:
    # ISO formats are checked first so a value that already canonicalizes
    # doesn't accidentally get reinterpreted (e.g. '2025-01-02' is
    # unambiguous as ISO; '01/02/2025' is ambiguous and we lean US M/D/Y
    # because the sample portal + most US enterprise CMSes use that).
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d %Y",
    "%B %d %Y",
)


def _decode_iso_date(raw: Any, param_name: str) -> str:
    if isinstance(raw, _dt.date) and not isinstance(raw, _dt.datetime):
        return raw.isoformat()
    if isinstance(raw, _dt.datetime):
        return raw.date().isoformat()
    s = str(raw).strip()
    for fmt in _ISO_DATE_INPUT_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ParamValidationError(
        param_name,
        f"value {s!r} is not a recognized date",
        {"value": s, "accepted_formats": list(_ISO_DATE_INPUT_FORMATS)},
    )


def _decode_iso_datetime(raw: Any, param_name: str) -> str:
    if isinstance(raw, _dt.datetime):
        return raw.isoformat()
    s = str(raw).strip()
    # Allow a date-only value to widen to midnight; the page-side input
    # likely accepts that.
    try:
        return _dt.datetime.fromisoformat(s).isoformat()
    except ValueError:
        pass
    try:
        # Common alternative: trailing Z (Zulu / UTC).
        if s.endswith("Z"):
            return _dt.datetime.fromisoformat(s[:-1] + "+00:00").isoformat()
    except ValueError:
        pass
    raise ParamValidationError(
        param_name,
        f"value {s!r} is not a recognized iso datetime",
        {"value": s},
    )


_TRUTHY: frozenset[str] = frozenset(
    {"true", "1", "yes", "on", "checked", "y", "t"}
)
_FALSY: frozenset[str] = frozenset(
    {"false", "0", "no", "off", "unchecked", "n", "f", ""}
)


def _decode_boolean(raw: Any, param_name: str) -> str:
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return "true" if raw != 0 else "false"
    s = str(raw).strip().lower()
    if s in _TRUTHY:
        return "true"
    if s in _FALSY:
        return "false"
    raise ParamValidationError(
        param_name,
        f"value {raw!r} is not a recognized boolean",
        {"value": raw, "truthy": sorted(_TRUTHY), "falsy": sorted(_FALSY)},
    )


def _decode_localized_number(raw: Any, param_name: str) -> str:
    """Parse a number that may carry locale-specific separators.

    Accepts common shapes: ``1,234.56`` (US/UK), ``1.234,56`` (DE), and
    plain ``1234.56``. Rejects strings with both ``,`` and ``.`` where
    we can't tell which is the decimal mark (operator should provide
    explicit locale_hint via PortalContext for those portals).

    The output is the canonical decimal string (``1234.56``) which is
    what HTML number inputs accept regardless of UI locale.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return str(raw)
    s = str(raw).strip()
    if not s:
        raise ParamValidationError(
            param_name, "empty value", {"value": raw}
        )
    has_comma = "," in s
    has_dot = "." in s
    if has_comma and has_dot:
        # Heuristic: whichever character appears LAST is the decimal.
        # ``1,234.56`` -> dot last -> US format -> drop commas.
        # ``1.234,56`` -> comma last -> EU format -> drop dots, swap.
        if s.rfind(".") > s.rfind(","):
            s2 = s.replace(",", "")
        else:
            s2 = s.replace(".", "").replace(",", ".")
    elif has_comma:
        # Ambiguous: ``1,234`` could be a US thousands or EU decimal.
        # Bias toward US because most enterprise portals are en-US;
        # operator can switch via codec on a per-param basis if needed.
        s2 = s.replace(",", "")
    else:
        s2 = s
    try:
        # ``float`` round-trips through canonical str
        return str(float(s2))
    except ValueError as e:
        raise ParamValidationError(
            param_name, f"value {raw!r} is not a number ({e})",
            {"value": raw},
        )


def _decode_enum_label(
    raw: Any,
    param_name: str,
    options: Optional[list[OptionSnapshot]],
) -> str:
    """Look up an option whose ``label`` matches the operator's input,
    return its ``value``. Falls back to a value-match for tolerance --
    operator can pass either label or value when codec=enum_label."""
    if options is None:
        raise ParamValidationError(
            param_name,
            "enum_label codec requires enum_options to be declared",
            {"value": raw},
        )
    s = str(raw).strip()
    # First pass: exact label match.
    for opt in options:
        if opt.label == s:
            return opt.value
    # Second pass: case-insensitive label.
    for opt in options:
        if opt.label.casefold() == s.casefold():
            return opt.value
    # Third pass: value match (operator already passed the canonical).
    for opt in options:
        if opt.value == s:
            return opt.value
    raise ParamValidationError(
        param_name,
        f"value {s!r} is not in the declared option set",
        {
            "value": s,
            "available_labels": [o.label for o in options],
            "available_values": [o.value for o in options],
        },
    )


def _decode_enum_value(
    raw: Any,
    param_name: str,
    options: Optional[list[OptionSnapshot]],
) -> str:
    s = str(raw).strip()
    if options is None:
        # No option set declared; pass through. Constraint check still
        # applies if allowed_values is set.
        return s
    for opt in options:
        if opt.value == s:
            return opt.value
    raise ParamValidationError(
        param_name,
        f"value {s!r} is not in the declared option set",
        {
            "value": s,
            "available_values": [o.value for o in options],
        },
    )


def _decode_file_ref(raw: Any, param_name: str) -> str:
    """Resolve a file_path against the local filesystem; fail-fast if
    missing. Returns the absolute path string the runner can hand to
    Playwright's ``set_input_files``.
    """
    s = str(raw).strip()
    if not s:
        raise ParamValidationError(
            param_name, "empty file path", {"value": raw}
        )
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = p.resolve()
    if not p.exists():
        raise ParamValidationError(
            param_name,
            f"file does not exist: {p}",
            {"value": s, "resolved": str(p)},
        )
    if not p.is_file():
        raise ParamValidationError(
            param_name,
            f"path is not a file: {p}",
            {"value": s, "resolved": str(p)},
        )
    return str(p)


def _decode_raw(raw: Any) -> Union[str, list[str]]:
    """Default pass-through. Lists stay lists (string_list); everything
    else becomes ``str`` so the page-side action handler sees a wire
    value, not a Python type."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, bool):
        # ``str(True)`` -> ``'True'`` which doesn't round-trip to HTML
        # checkbox semantics; normalize to lowercase.
        return "true" if raw else "false"
    return str(raw)


# ---------------------------------------------------------------------------
# Constraint validation
# ---------------------------------------------------------------------------


def _validate_constraints(
    value: Union[str, list[str]],
    param: SkillParam,
) -> None:
    """Apply ParamConstraints. Raises ParamValidationError on failure.

    Constraint order matters because earlier failures are cheaper to
    explain: allowed_values is a single-set check, regex compile only
    fires when shape is declared, file checks come last because they
    only apply to file_path types.
    """
    c = param.constraints
    if c is None:
        return

    if isinstance(value, list):
        if c.list_min_len is not None and len(value) < c.list_min_len:
            raise ParamValidationError(
                param.name,
                f"list has {len(value)} items, minimum {c.list_min_len}",
                {"value": value, "min": c.list_min_len},
            )
        if c.list_max_len is not None and len(value) > c.list_max_len:
            raise ParamValidationError(
                param.name,
                f"list has {len(value)} items, maximum {c.list_max_len}",
                {"value": value, "max": c.list_max_len},
            )
        # Per-item allowed_values check (each item must be in the set).
        if c.allowed_values is not None:
            allowed = set(c.allowed_values)
            for item in value:
                if item not in allowed:
                    raise ParamValidationError(
                        param.name,
                        f"item {item!r} not in allowed_values",
                        {"value": item, "allowed": c.allowed_values},
                    )
        return

    # Scalar checks beyond this point.
    if c.allowed_values is not None and value not in c.allowed_values:
        raise ParamValidationError(
            param.name,
            f"value {value!r} not in allowed_values",
            {"value": value, "allowed": c.allowed_values},
        )

    if c.min is not None or c.max is not None:
        # min/max are inclusive. For dates we string-compare (ISO sorts
        # lexicographically); for numbers we try float; otherwise we
        # fall back to lex comparison.
        if param.type in ("number", "number_range"):
            try:
                v = float(value)
            except ValueError as e:
                raise ParamValidationError(
                    param.name,
                    f"value {value!r} not a number ({e})",
                    {"value": value},
                )
            if c.min is not None and v < c.min:
                raise ParamValidationError(
                    param.name,
                    f"value {v} below min {c.min}",
                    {"value": v, "min": c.min},
                )
            if c.max is not None and v > c.max:
                raise ParamValidationError(
                    param.name,
                    f"value {v} above max {c.max}",
                    {"value": v, "max": c.max},
                )
        else:
            # Lex comparison covers ISO dates / datetimes.
            if c.min is not None and value < str(c.min):
                raise ParamValidationError(
                    param.name,
                    f"value {value!r} below min {c.min!r}",
                    {"value": value, "min": c.min},
                )
            if c.max is not None and value > str(c.max):
                raise ParamValidationError(
                    param.name,
                    f"value {value!r} above max {c.max!r}",
                    {"value": value, "max": c.max},
                )

    if c.required_shape:
        try:
            pattern = re.compile(c.required_shape)
        except re.error as e:
            raise ParamValidationError(
                param.name,
                f"required_shape regex invalid ({e})",
                {"required_shape": c.required_shape},
            )
        if not pattern.fullmatch(value):
            raise ParamValidationError(
                param.name,
                f"value {value!r} does not match required_shape",
                {
                    "value": value,
                    "required_shape": c.required_shape,
                },
            )

    if param.type == "file_path":
        # Only validated when the codec already resolved the path; raw
        # mode skips this since the operator may pass a string the
        # runner does additional handling on.
        if c.mime_types or c.extensions:
            ext = Path(value).suffix.lower()
            if c.extensions and ext not in c.extensions:
                raise ParamValidationError(
                    param.name,
                    f"extension {ext!r} not in allowed list",
                    {"value": value, "extensions": c.extensions},
                )
            if c.mime_types:
                guessed, _ = mimetypes.guess_type(value)
                if not any(
                    (guessed or "").startswith(p) for p in c.mime_types
                ):
                    raise ParamValidationError(
                        param.name,
                        (
                            f"mime type {guessed!r} not in allowed prefixes"
                        ),
                        {
                            "value": value,
                            "guessed_mime": guessed,
                            "mime_prefixes": c.mime_types,
                        },
                    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_param(
    param: SkillParam,
    raw_value: Any,
) -> Union[str, list[str]]:
    """Resolve ``raw_value`` through ``param``'s codec, then validate
    against its constraints. Returns the canonical wire value the page
    expects (string for scalars, list[str] for string_list).

    Raises ``ParamValidationError`` on any codec or constraint failure.
    The caller (skill_runner) converts that into
    ``ToolResult.error_kind="param_validation_failed"`` and skips the
    page action.

    Legacy v1 skills with no codec field default to ``raw`` and behave
    identically to the previous str-coercion path.
    """
    codec = param.codec or "raw"

    # string_list / object short-circuit: codec doesn't apply per-item
    # transformation today (future WI may add a per-item codec field).
    if param.type == "string_list":
        if isinstance(raw_value, str):
            # Operator may have passed a comma-separated string; split
            # on commas with trim. Single non-empty item stays as one.
            items = [s.strip() for s in raw_value.split(",") if s.strip()]
        elif isinstance(raw_value, (list, tuple)):
            items = [str(x) for x in raw_value]
        else:
            items = [str(raw_value)]
        _validate_constraints(items, param)
        return items
    if param.type == "object":
        # Object params pass through unchanged today. Tests cover that
        # the codec doesn't mangle nested structures; future WI may
        # serialize for the runner.
        if isinstance(raw_value, str):
            return raw_value
        # Render anything else through JSON for stable wire shape.
        import json

        return json.dumps(raw_value)

    if codec == "iso_date":
        out = _decode_iso_date(raw_value, param.name)
    elif codec == "iso_datetime":
        out = _decode_iso_datetime(raw_value, param.name)
    elif codec == "boolean":
        out = _decode_boolean(raw_value, param.name)
    elif codec == "localized_number":
        out = _decode_localized_number(raw_value, param.name)
    elif codec == "enum_label":
        out = _decode_enum_label(raw_value, param.name, param.enum_options)
    elif codec == "enum_value":
        out = _decode_enum_value(raw_value, param.name, param.enum_options)
    elif codec == "file_ref":
        out = _decode_file_ref(raw_value, param.name)
    else:
        # ``raw`` or any future codec we don't know about.
        decoded = _decode_raw(raw_value)
        out = decoded if isinstance(decoded, str) else str(decoded)

    _validate_constraints(out, param)
    return out


# ---------------------------------------------------------------------------
# Annotator helpers: infer type + codec from grabber-recorded metadata
# ---------------------------------------------------------------------------


def infer_param_type_and_codec(
    *,
    control_kind: Optional[str],
    value_kind: Optional[str],
    recorded_value: Optional[Any],
    has_options_snapshot: bool,
) -> tuple[str, str]:
    """Map ``(control_kind, value_kind, recorded_value)`` -- captured by
    the grabber at record time -- to a ``(SkillParam.type, codec)`` pair.

    This is the deterministic inference the annotator uses to upgrade
    legacy ``string`` params into typed ones (WI-05). It does NOT
    consult the LLM; structural truth lives in the recording, the LLM
    only renames params.

    Falls back to (``string``, ``raw``) when the recorder didn't capture
    enough metadata (legacy traces); the runner remains correct in that
    case, just less safe.
    """
    ck = (control_kind or "").lower()
    vk = (value_kind or "").lower()

    if ck in ("checkbox", "radio") or vk == "boolean":
        return ("boolean", "boolean")
    if ck == "select_single" and has_options_snapshot:
        return ("enum", "enum_value")
    if ck == "select_multiple" or ck == "listbox_aria":
        return ("string_list", "raw")
    if ck == "range_slider" or vk == "number":
        return ("number_range" if ck == "range_slider" else "number",
                "localized_number")
    if ck in ("number_input",):
        return ("number", "localized_number")
    if ck in ("date_input", "month_input", "week_input"):
        return ("date", "iso_date")
    if ck in ("datetime_input", "time_input") or vk == "datetime":
        return ("datetime", "iso_datetime")
    if ck == "file_input" or vk == "file":
        return ("file_path", "file_ref")
    if ck == "contenteditable" or vk == "html":
        return ("object", "raw")
    # Default: leave as untyped string. The runner accepts that.
    return ("string", "raw")
