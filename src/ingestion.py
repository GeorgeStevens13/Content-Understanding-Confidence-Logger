"""
Parse Azure AI Content Understanding output and flatten it into rows ready for
the EAV `cu.DocumentFields` table.

Handles two input shapes:

  A) Analyze-API result (production):
       { "id": "...", "status": "...", "result": {
           "analyzerId": "...", "apiVersion": "...", "createdAt": "...",
           "contents": [ { "path": "input1", "fields": {...}, ... } ] } }

  B) Labels file (training):
       { "$schema": ".../labels.json", "fieldLabels": {...},
         "metadata": { "displayName": "...", "createdDateTime": "...",
                       "mimeType": "..." } }

Each LEAF field becomes one `FieldRow`. Nested objects flatten to
"Parent.Child" paths; array elements use "Parent[idx].Child".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Value type handling
# ---------------------------------------------------------------------------

# Leaf field types CU emits and the key under which the value is stored.
# Anything not in this map and not "object"/"array" is treated as a string leaf.
_VALUE_KEY = {
    "string":   "valueString",
    "number":   "valueNumber",
    "integer":  "valueInteger",
    "date":     "valueDate",
    "time":     "valueTime",
    "boolean":  "valueBoolean",
    "currency": "valueCurrency",   # { amount, currencyCode, currencySymbol }
    "address":  "valueAddress",    # { streetAddress, city, ... }
    "selectionMark": "valueSelectionMark",
}


@dataclass
class FieldRow:
    field_path: str
    field_name: str
    parent_path: str | None
    array_index: int | None
    field_type: str
    value_string: str | None = None
    value_number: float | None = None
    value_integer: int | None = None
    value_date: str | None = None        # ISO yyyy-mm-dd
    value_boolean: bool | None = None
    currency_code: str | None = None
    confidence: float | None = None
    span_offset: int | None = None
    span_length: int | None = None


@dataclass
class ParsedDocument:
    """One logical document = one entry in `cu.Documents`."""
    content_path: str                # e.g. "input1" / "labels"
    document_name: str
    analyzer_id: str | None
    api_version: str | None
    operation_id: str | None
    status: str | None
    source_created_at: datetime | None
    mime_type: str | None
    fields: list[FieldRow] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_content_understanding_json(
    payload: dict[str, Any],
    *,
    default_document_name: str,
) -> list[ParsedDocument]:
    """Return one ParsedDocument per logical document found in `payload`."""

    # Format A — analyze result
    if "result" in payload and isinstance(payload.get("result"), dict):
        return _parse_analyze_result(payload, default_document_name)

    # Format B — labels file
    if "fieldLabels" in payload:
        return [_parse_labels_file(payload, default_document_name)]

    raise ValueError(
        "Unrecognized JSON shape: expected either Content Understanding "
        "analyze result (with 'result.contents') or labels file (with "
        "'fieldLabels')."
    )


# ---------------------------------------------------------------------------
# Format A — analyze result
# ---------------------------------------------------------------------------

def _parse_analyze_result(
    payload: dict[str, Any],
    default_document_name: str,
) -> list[ParsedDocument]:
    result = payload.get("result") or {}
    contents = result.get("contents") or []
    if not contents:
        raise ValueError("Analyze result contains no 'contents' entries.")

    analyzer_id   = result.get("analyzerId")
    api_version   = result.get("apiVersion")
    operation_id  = payload.get("id")
    status        = payload.get("status")
    created_at    = _parse_iso_datetime(result.get("createdAt"))

    out: list[ParsedDocument] = []
    for idx, content in enumerate(contents):
        if not isinstance(content, dict):
            continue
        content_path = content.get("path") or f"input{idx + 1}"
        doc = ParsedDocument(
            content_path=str(content_path),
            document_name=default_document_name,
            analyzer_id=analyzer_id,
            api_version=api_version,
            operation_id=operation_id,
            status=status,
            source_created_at=created_at,
            mime_type=None,
        )
        fields = content.get("fields") or {}
        doc.fields = list(_walk_fields(fields, parent_path=None, array_index=None))
        out.append(doc)
    return out


# ---------------------------------------------------------------------------
# Format B — labels file
# ---------------------------------------------------------------------------

def _parse_labels_file(
    payload: dict[str, Any],
    default_document_name: str,
) -> ParsedDocument:
    meta = payload.get("metadata") or {}
    doc = ParsedDocument(
        content_path="labels",
        document_name=meta.get("displayName") or default_document_name,
        analyzer_id=None,
        api_version=payload.get("$schema"),
        operation_id=payload.get("fileId") or None,
        status=None,
        source_created_at=_parse_iso_datetime(meta.get("createdDateTime")),
        mime_type=meta.get("mimeType"),
    )
    field_labels = payload.get("fieldLabels") or {}
    doc.fields = list(_walk_fields(field_labels, parent_path=None, array_index=None))
    return doc


# ---------------------------------------------------------------------------
# Recursive walker — works for both formats since the field-node shape is
# identical (type + value<Type> + confidence + spans + optional valueObject /
# valueArray).
# ---------------------------------------------------------------------------

def _walk_fields(
    fields: dict[str, Any],
    *,
    parent_path: str | None,
    array_index: int | None,
) -> Iterable[FieldRow]:
    for name, node in fields.items():
        if not isinstance(node, dict):
            continue
        path = f"{parent_path}.{name}" if parent_path else name
        yield from _walk_node(node, path=path, name=name,
                              parent_path=parent_path, array_index=array_index)


def _walk_node(
    node: dict[str, Any],
    *,
    path: str,
    name: str,
    parent_path: str | None,
    array_index: int | None,
) -> Iterable[FieldRow]:
    ftype = node.get("type") or "string"

    # Container: object -> recurse into valueObject
    if ftype == "object":
        sub = node.get("valueObject")
        if isinstance(sub, dict):
            yield from _walk_fields(sub, parent_path=path, array_index=array_index)
        return

    # Container: array -> recurse into valueArray, indexed
    if ftype == "array":
        items = node.get("valueArray") or []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            indexed_path = f"{path}[{i}]"
            sub_type = item.get("type")
            # Array items are normally typed objects whose children are the leaves.
            if sub_type == "object":
                sub = item.get("valueObject") or {}
                yield from _walk_fields(sub, parent_path=indexed_path, array_index=i)
            else:
                # Rare: array of scalars. Treat the item itself as a leaf.
                yield from _walk_node(
                    item,
                    path=indexed_path,
                    name=f"{name}[{i}]",
                    parent_path=path,
                    array_index=i,
                )
        return

    # Leaf — build a FieldRow
    yield _build_leaf(node, path=path, name=name,
                      parent_path=parent_path, array_index=array_index,
                      ftype=ftype)


def _build_leaf(
    node: dict[str, Any],
    *,
    path: str,
    name: str,
    parent_path: str | None,
    array_index: int | None,
    ftype: str,
) -> FieldRow:
    row = FieldRow(
        field_path=path,
        field_name=name,
        parent_path=parent_path,
        array_index=array_index,
        field_type=ftype,
        confidence=_safe_float(node.get("confidence")),
    )

    # Span (first only — useful for joining back to the markdown/text).
    spans = node.get("spans") or []
    if spans and isinstance(spans[0], dict):
        row.span_offset = _safe_int(spans[0].get("offset"))
        row.span_length = _safe_int(spans[0].get("length"))

    # Extract typed values. value_string is always populated as a fallback.
    value_key = _VALUE_KEY.get(ftype)
    raw = node.get(value_key) if value_key else None

    if ftype == "string":
        row.value_string = _to_str(raw)
    elif ftype == "number":
        row.value_number = _safe_float(raw)
        row.value_string = _to_str(raw)
    elif ftype == "integer":
        row.value_integer = _safe_int(raw)
        row.value_number  = _safe_float(raw)   # convenient for Power BI
        row.value_string  = _to_str(raw)
    elif ftype == "date":
        row.value_date    = _to_str(raw)       # already ISO yyyy-mm-dd
        row.value_string  = _to_str(raw)
    elif ftype == "time":
        row.value_string  = _to_str(raw)
    elif ftype == "boolean":
        row.value_boolean = bool(raw) if raw is not None else None
        row.value_string  = _to_str(raw)
    elif ftype == "currency" and isinstance(raw, dict):
        row.value_number   = _safe_float(raw.get("amount"))
        row.currency_code  = _to_str(raw.get("currencyCode"))
        row.value_string   = f"{raw.get('amount')} {raw.get('currencyCode', '')}".strip()
    elif ftype == "address" and isinstance(raw, dict):
        # Flatten address to a readable single string; details available via spans.
        parts = [raw.get(k) for k in
                 ("streetAddress", "city", "state", "postalCode", "countryRegion")]
        row.value_string = ", ".join(p for p in parts if p)
    elif ftype == "selectionMark":
        row.value_string = _to_str(raw)
    else:
        # Unknown type — keep a stringified version so nothing is lost.
        row.value_string = _to_str(raw if raw is not None else node.get("valueString"))

    # Final safety net: if we still have nothing but a confidence, store an empty string
    # rather than leaving everything NULL so it's obviously a "present but empty" leaf.
    if (
        row.value_string is None
        and row.value_number is None
        and row.value_integer is None
        and row.value_date is None
        and row.value_boolean is None
        and row.confidence is not None
    ):
        row.value_string = ""

    return row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return str(v)


def _parse_iso_datetime(v: Any) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    s = v.strip()
    # Python <3.11 doesn't accept trailing "Z" in fromisoformat; normalise.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
