export function applyOrderUpdateToOrder<T extends { order_id: number }>(
  order: T,
  msg: any
): T {
  if (!msg || msg.order_id !== order.order_id) {
    return order;
  }

  const next: any = { ...order };

  if (typeof msg.status === 'string') next.status = msg.status;

  if (typeof msg.total_items === 'number') next.total_items = msg.total_items;
  if (typeof msg.completed_items === 'number') next.completed_items = msg.completed_items;
  if (typeof msg.failed_items === 'number') next.failed_items = msg.failed_items;

  if (typeof msg.total_attachments === 'number') next.total_attachments = msg.total_attachments;
  if (typeof msg.completed_attachments === 'number')
    next.completed_attachments = msg.completed_attachments;
  if (typeof msg.failed_attachments === 'number')
    next.failed_attachments = msg.failed_attachments;

  if (typeof msg.updated_at === 'string') next.updated_at = msg.updated_at;

  // Optional fields used by the order details page
  if (typeof msg.final_report_paths !== 'undefined') next.final_report_paths = msg.final_report_paths;
  if (typeof msg.remap_item_count !== 'undefined') next.remap_item_count = msg.remap_item_count;
  if (typeof msg.can_remap !== 'undefined') next.can_remap = msg.can_remap;

  return next as T;
}

