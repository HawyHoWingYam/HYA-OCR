"""Legacy MappingConfigResolver has been removed.

This module is kept as an empty placeholder to avoid import errors during the
transition away from MappingTemplate / CompanyDocMappingDefault. All mapping
resolution should now go through CompanyDocTypeConfigResolver and/or explicit
item.mapping_config payloads.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session  # noqa: F401

from utils.mapping_config import ResolvedMappingConfig  # noqa: F401


class MappingConfigResolver:  # pragma: no cover - legacy shim
    """Shim that raises at runtime; do not use in new code."""

    def __init__(self, db: Session):
        self.db = db

    def resolve_for_item(
        self,
        *,
        company_id: int,
        doc_type_id: int,
        item_type: Any,
        current_config: Optional[Dict[str, Any]],
    ) -> Optional[ResolvedMappingConfig]:
        raise RuntimeError(
            "MappingConfigResolver is deprecated. Use CompanyDocTypeConfigResolver "
            "and company_doc_type_configs instead."
        )
