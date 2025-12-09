"""Resolve mapping configuration from company_doc_type_configs.

This resolver prefers the new company_doc_type_configs table as the single
source of truth for per-company + document-type + item-type mapping settings.
Legacy MappingTemplate / CompanyDocMappingDefault resolution is handled
elsewhere (and may be phased out over time).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from db.models import CompanyDocTypeConfig, OrderItemType
from utils.mapping_config import (
    MappingItemType,
    merge_mapping_configs,
    normalise_mapping_config,
)


class CompanyDocTypeConfigResolver:
    """Determine effective mapping configuration from CompanyDocTypeConfig."""

    @staticmethod
    def get_active_row(
        db: Session,
        company_id: Optional[int],
        doc_type_id: Optional[int],
        item_type: Optional[OrderItemType] = None,
    ) -> Optional[CompanyDocTypeConfig]:
        """Return the active CompanyDocTypeConfig row for provided identifiers."""
        if db is None or not company_id or not doc_type_id:
            return None

        def _normalise_item_type(value: Optional[OrderItemType]) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, OrderItemType):
                return value.value
            try:
                return OrderItemType(value).value  # type: ignore[arg-type]
            except Exception:
                if isinstance(value, str):
                    return value
                return None

        resolved_item_type = _normalise_item_type(item_type)

        base_query = (
            db.query(CompanyDocTypeConfig)
            .filter(
                CompanyDocTypeConfig.company_id == company_id,
                CompanyDocTypeConfig.doc_type_id == doc_type_id,
                CompanyDocTypeConfig.active.is_(True),
            )
        )

        order_clause = (
            CompanyDocTypeConfig.priority.asc(),
            CompanyDocTypeConfig.config_id.asc(),
        )

        if resolved_item_type:
            targeted = base_query.filter(CompanyDocTypeConfig.item_type == resolved_item_type)
            row = targeted.order_by(*order_clause).first()
            if row:
                return row

        rows = base_query.order_by(*order_clause).all()
        if not rows:
            return None

        def _item_type_weight(row: CompanyDocTypeConfig) -> int:
            value = getattr(row, "item_type", None)
            if value == OrderItemType.SINGLE_SOURCE.value:
                return 0
            if value == OrderItemType.MULTI_SOURCE.value:
                return 1
            return 2

        rows.sort(key=lambda row: ((row.priority or 100), _item_type_weight(row), row.config_id))
        return rows[0]

    def __init__(self, db: Session):
        self.db = db

    def _find_config(
        self,
        *,
        company_id: int,
        doc_type_id: int,
        item_type: OrderItemType,
    ) -> Optional[CompanyDocTypeConfig]:
        """Return the active config row for (company, doc_type, item_type) if any."""

        return (
            self.db.query(CompanyDocTypeConfig)
            .filter(
                CompanyDocTypeConfig.company_id == company_id,
                CompanyDocTypeConfig.doc_type_id == doc_type_id,
                CompanyDocTypeConfig.item_type == item_type.value,
                CompanyDocTypeConfig.active.is_(True),
            )
            .order_by(CompanyDocTypeConfig.priority.asc(), CompanyDocTypeConfig.config_id.asc())
            .first()
        )

    def resolve_for_item(
        self,
        *,
        company_id: int,
        doc_type_id: int,
        item_type: OrderItemType,
        current_config: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Resolve defaults for an order item and return a normalised mapping_config dict.

        Behaviour:
        - If there is no CompanyDocTypeConfig row -> return None (caller may fall back
          to legacy resolution or manual config).
        - Otherwise, build a base payload from the config row, then deep-merge any
          current_config override, and finally run it through normalise_mapping_config
          for validation and cleanup.
        """

        row = self._find_config(
            company_id=company_id,
            doc_type_id=doc_type_id,
            item_type=item_type,
        )
        if not row:
            return None

        mapping_item_type = MappingItemType(item_type.value)
        base_payload: Dict[str, Any] = {}

        # Defensive copy of current_config so we can safely normalise it before merge.
        # In particular, treat an explicit empty list for external_join_keys as "no override"
        # so that company-level defaults (e.g. ["service_number"]) are not accidentally
        # disabled when the item UI sends [].
        override_payload: Dict[str, Any] = dict(current_config or {})
        ek = override_payload.get("external_join_keys")
        if isinstance(ek, list) and not [k for k in ek if str(k).strip()]:
            override_payload.pop("external_join_keys", None)

        if mapping_item_type == MappingItemType.SINGLE_SOURCE:
            # Start from stored single_source_config, then ensure master_csv_path is present
            base_payload = dict(row.single_source_config or {})
            if row.master_csv_path and "master_csv_path" not in base_payload:
                base_payload["master_csv_path"] = row.master_csv_path
        elif mapping_item_type == MappingItemType.MULTI_SOURCE:
            # For multi_source, use step2 config as the external join to master CSV.
            # Step1 (OCR -> month Excel) is handled at mapping time and does not need
            # to be pushed into item.mapping_config for now.
            step2_cfg = row.multi_source_step2_config or {}
            base_payload = {}

            if row.master_csv_path:
                base_payload["master_csv_path"] = row.master_csv_path

            join_keys = step2_cfg.get("join_keys")
            if join_keys:
                base_payload["external_join_keys"] = join_keys

            for key in ("column_aliases", "join_normalize", "output_meta", "merge_suffix"):
                if key in step2_cfg and step2_cfg[key] is not None:
                    base_payload[key] = step2_cfg[key]

            if row.internal_join_key:
                base_payload["internal_join_key"] = row.internal_join_key
            if row.attachment_sources:
                base_payload["attachment_sources"] = row.attachment_sources
        else:  # pragma: no cover - guarded by enum
            return None

        merged_payload = merge_mapping_configs(base_payload, override_payload)

        # normalise_mapping_config performs strict validation (master_csv_path presence,
        # join_normalize/output_meta structure, etc.) and returns a clean dict.
        normalised = normalise_mapping_config(mapping_item_type, merged_payload)
        return normalised
