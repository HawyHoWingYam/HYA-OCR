from typing import Dict, List, Optional, Any

from sqlalchemy.orm import Session

from db.models import OcrOrder, OcrOrderItem, OrderItemStatus


def compute_order_attachment_stats(order: OcrOrder, db: Session) -> Dict[str, int]:
    """
    Compute attachment-level statistics for a given order.

    Attachment counts are derived from OcrOrderItem.file_count, which represents
    the number of attachment files for an item (excluding the primary file).
    """
    total_attachments = 0
    completed_attachments = 0
    failed_attachments = 0

    # Use relationship collection if already loaded; otherwise query explicitly
    items: List[OcrOrderItem]
    if hasattr(order, "items") and order.items is not None:
        items = list(order.items)
    else:
        items = (
            db.query(OcrOrderItem)
            .filter(OcrOrderItem.order_id == order.order_id)
            .all()
        )

    for item in items:
        # file_count tracks attachments only (primary file is excluded)
        attachments_for_item = item.file_count or 0
        if attachments_for_item <= 0:
            continue

        total_attachments += attachments_for_item

        if item.status == OrderItemStatus.COMPLETED:
            completed_attachments += attachments_for_item
        elif item.status == OrderItemStatus.FAILED:
            failed_attachments += attachments_for_item

    return {
        "total_attachments": total_attachments,
        "completed_attachments": completed_attachments,
        "failed_attachments": failed_attachments,
    }


def build_order_update_payload(
    order: OcrOrder,
    db: Session,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Build a standardised order_update payload for WebSocket broadcasts.

    This helper ensures that all order_update messages share the same
    core shape and that attachment statistics are computed consistently.
    """
    attachment_stats = compute_order_attachment_stats(order, db)

    payload: Dict[str, Any] = {
        "type": "order_update",
        "order_id": order.order_id,
        "status": order.status.value,
        "total_items": order.total_items,
        "completed_items": order.completed_items,
        "failed_items": order.failed_items,
        "total_attachments": attachment_stats["total_attachments"],
        "completed_attachments": attachment_stats["completed_attachments"],
        "failed_attachments": attachment_stats["failed_attachments"],
    }

    if extra:
        payload.update(extra)

    return payload

