export const ADMIN_ROWS_PER_PAGE = 75;

/** Keep the Admin DOM bounded while retaining each group's full membership.
 * `items` is what should render; `allItems` is what group selection targets.
 */
export function limitGroupedRows(groups, requestedLimit) {
  let remaining = Math.max(0, Number(requestedLimit) || 0);
  const visible = [];
  for (const group of groups) {
    if (remaining <= 0) break;
    const items = group.items.slice(0, remaining);
    if (items.length === 0) continue;
    visible.push({
      ...group,
      items,
      allItems: group.items,
      totalCount: group.items.length,
    });
    remaining -= items.length;
  }
  return visible;
}
