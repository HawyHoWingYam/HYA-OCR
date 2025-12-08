"""Helpers for validating and merging mapping configuration payloads."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, validator

# Master CSV root path - all master CSV paths must start with this
MASTER_CSV_ROOT = "HYA-OCR/Master Data"


class MappingItemType(str, Enum):
    """Supported mapping modes."""

    SINGLE_SOURCE = "single_source"
    MULTI_SOURCE = "multi_source"


class AttachmentSource(BaseModel):
    """Attachment source definition and optional join key override.

    Note: While 'path' is intended for OneDrive folder ingestion, attachments may also be
    linked manually. For robust matching in processing, an optional 'filename_contains'
    can be provided so attachment rows can resolve to the correct join key by filename.
    If 'join_key' is not provided per source, the multi-source 'internal_join_key' will
    be used as a global default.
    """

    kind: Literal["onedrive"] = Field(
        ..., description="Attachment source type (onedrive only for now)",
    )
    path: str = Field(..., description="OneDrive folder path that stores the PDF attachments")
    label: Optional[str] = Field(
        default=None, description="Optional human friendly label shown in UI"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Extra parameters (e.g. month='202510', doc_type='OCS').",
    )
    # New: allow per-source join key override (e.g. some attachments join on 'invoice_no', others on 'account_id')
    join_key: Optional[str] = Field(
        default=None, description="Optional join key for this attachment group"
    )
    # New: fallback filename matcher when 'path' is not available on records; used against '__filename' metadata
    filename_contains: Optional[str] = Field(
        default=None, description="Case-insensitive substring to match attachment filenames to this source"
    )

    @validator("path")
    def _validate_path(cls, value: str) -> str:  # pylint: disable=no-self-argument
        if not value:
            raise ValueError("Attachment source path cannot be empty")
        return value


class BaseMappingConfig(BaseModel):
    master_csv_path: str = Field(
        ..., description="Absolute OneDrive path to the master CSV used for the final join",
    )
    external_join_keys: List[str] = Field(
        default_factory=list,
        description="Ordered list of join keys between OCR data and master CSV",
    )
    column_aliases: Dict[str, str] = Field(
        default_factory=dict,
        description="Optional mapping of OCR column names to CSV column names",
    )
    notes: Optional[str] = Field(
        default=None, description="Optional free-form notes for operators",
    )
    join_normalize: Optional[dict] = Field(
        default=None,
        description="Join value normalization options: {strip_non_digits: bool, zfill: int | {key: int}}",
    )
    output_meta: Optional[dict] = Field(
        default=None,
        description="Output column mapping: {dest: 'ctx:order_id' | 'col:__item_id', ...}",
    )
    merge_suffix: Optional[str] = Field(
        default=None,
        description="Suffix for conflicting column names from master CSV (default '_master')",
    )
    join_normalize: Optional[dict] = Field(
        default=None,
        description="Join value normalization options: {strip_non_digits: bool, zfill: int | {key: int}}",
    )
    output_meta: Optional[dict] = Field(
        default=None,
        description="Output column mapping: {dest: 'ctx:order_id' | 'col:__item_id', ...}",
    )

    @validator("master_csv_path")
    def _validate_master_path(cls, value: str) -> str:  # pylint: disable=no-self-argument
        if not value:
            raise ValueError("master_csv_path is required")
        value = value.strip()
        if not (value.startswith(MASTER_CSV_ROOT + "/") or value == MASTER_CSV_ROOT):
            raise ValueError(f"master_csv_path must start with '{MASTER_CSV_ROOT}'")
        return value


class SingleSourceMappingConfig(BaseMappingConfig):
    item_type: MappingItemType = Field(  # type: ignore[assignment]
        default=MappingItemType.SINGLE_SOURCE,
        description="Mapping mode",
    )


class MultiSourceMappingConfig(BaseMappingConfig):
    item_type: MappingItemType = Field(  # type: ignore[assignment]
        default=MappingItemType.MULTI_SOURCE,
        description="Mapping mode",
    )
    # Optional global default; if omitted, each attachment source should provide its own 'join_key'
    internal_join_key: Optional[str] = Field(
        default=None,
        description="Default join key between primary and attachments; per-source 'join_key' overrides when provided",
    )
    attachment_sources: List[AttachmentSource] = Field(
        default_factory=list,
        description="List of attachment source definitions (e.g. OneDrive folders by month)",
    )

    @validator("attachment_sources")
    def _validate_sources(
        cls, value: List[AttachmentSource], values
    ) -> List[AttachmentSource]:  # pylint: disable=no-self-argument
        # Allow empty when a default internal_join_key is provided; otherwise require at least one rule
        default_key = values.get("internal_join_key") if isinstance(values, dict) else None
        if not value and not default_key:
            raise ValueError("At least one attachment source or a default internal_join_key is required for multi-source mapping")
        return value

    @validator("attachment_sources")
    def _validate_join_keys(cls, sources: List[AttachmentSource], values):  # pylint: disable=no-self-argument
        """Ensure at least one usable join key is present: either a global default or per-source overrides."""
        default_key = values.get("internal_join_key") if isinstance(values, dict) else None
        if not default_key:
            # No global default: require every source to set join_key
            missing = [src for src in sources if not getattr(src, "join_key", None)]
            if missing:
                raise ValueError(
                    "When 'internal_join_key' is not provided, each attachment source must provide a 'join_key'"
                )
        return sources


def normalise_mapping_config(
    item_type: MappingItemType, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Validate and normalise a raw mapping config payload for persistence."""

    if item_type == MappingItemType.SINGLE_SOURCE:
        model = SingleSourceMappingConfig(**payload)
    elif item_type == MappingItemType.MULTI_SOURCE:
        model = MultiSourceMappingConfig(**payload)
    else:  # pragma: no cover - guarded by enum
        raise ValueError(f"Unsupported mapping item type: {item_type}")

    # use dict() with exclude_none to keep payload lean and serialisable
    data = model.model_dump(exclude_none=True)

    # Validate join_normalize
    jn = data.get("join_normalize")
    if isinstance(jn, dict):
        snd = jn.get("strip_non_digits")
        if snd is not None and not isinstance(snd, bool):
            raise ValueError("join_normalize.strip_non_digits must be a boolean")
        zf = jn.get("zfill")
        if zf is not None:
            if isinstance(zf, int):
                if zf < 0:
                    raise ValueError("join_normalize.zfill must be >= 0")
            elif isinstance(zf, dict):
                for k, v in zf.items():
                    if not isinstance(k, str) or not isinstance(v, int) or v < 0:
                        raise ValueError("join_normalize.zfill per-key map must be {str: int>=0}")
            else:
                raise ValueError("join_normalize.zfill must be int or {key:int}")
    elif jn is not None:
        raise ValueError("join_normalize must be an object")

    # Validate output_meta
    om = data.get("output_meta")
    if isinstance(om, dict):
        for dest, src in om.items():
            if not isinstance(dest, str) or not dest:
                raise ValueError("output_meta keys must be non-empty strings")
            if not isinstance(src, str) or not (src.startswith("ctx:") or src.startswith("col:")):
                raise ValueError("output_meta values must start with 'ctx:' or 'col:'")
    elif om is not None:
        raise ValueError("output_meta must be an object mapping dest->'ctx:'|'col:' source")

    # Validate merge_suffix
    ms = data.get("merge_suffix")
    if ms is not None and (not isinstance(ms, str) or len(ms) > 32):
        raise ValueError("merge_suffix must be a short string (<=32 chars)")

    return data


def merge_mapping_configs(
    base: Optional[Dict[str, Any]], override: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Simple deep merge helper for mapping config dictionaries."""

    def _merge_dict(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = dict(a)
        for key, value in b.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = _merge_dict(result[key], value)
            else:
                result[key] = value
        return result

    base_dict = base or {}
    override_dict = override or {}
    return _merge_dict(base_dict, override_dict)


def _diff_mapping_configs(base: Dict[str, Any], merged: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the minimal override required to transform base into merged."""

    diff: Dict[str, Any] = {}
    keys = set(base.keys()) | set(merged.keys())

    for key in keys:
        base_present = key in base
        merged_present = key in merged
        base_value = base[key] if base_present else None

        if merged_present:
            merged_value = merged[key]
            if isinstance(base_value, dict) and isinstance(merged_value, dict):
                nested_diff = _diff_mapping_configs(base_value, merged_value)
                if nested_diff:
                    diff[key] = nested_diff
            elif isinstance(base_value, list) and isinstance(merged_value, list):
                if merged_value != base_value:
                    diff[key] = merged_value
            else:
                if merged_value != base_value:
                    diff[key] = merged_value
        elif base_present:
            diff[key] = None

    return diff


def normalise_mapping_override(
    item_type: MappingItemType,
    override_payload: Dict[str, Any],
    *,
    template_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate override payloads while allowing sparse diffs against a template."""

    template_config = template_config or {}

    if not template_config:
        # No template available; override must be a complete payload.
        return normalise_mapping_config(item_type, override_payload)

    base_normalised = normalise_mapping_config(item_type, template_config)
    merged_payload = merge_mapping_configs(template_config, override_payload)
    merged_normalised = normalise_mapping_config(item_type, merged_payload)

    return _diff_mapping_configs(base_normalised, merged_normalised)


@dataclass
class ResolvedMappingConfig:
    config: Dict[str, Any]
    template_id: Optional[int]
    source: str
