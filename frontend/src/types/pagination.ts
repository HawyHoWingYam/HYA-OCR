/**
 * Shared pagination metadata interface used across all paginated list pages
 */
export interface PaginationInfo {
  total_count: number;
  total_pages: number;
  current_page: number;
  page_size: number;
  has_next: boolean;
  has_prev: boolean;
}
