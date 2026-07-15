export function progressiveRestaurantSections({
  onMap,
  offMap,
  filtered,
  visibleLimit,
  selectedRestaurantId,
}) {
  const visibleOnMap = onMap.slice(0, visibleLimit);
  const remaining = Math.max(0, visibleLimit - visibleOnMap.length);
  const visibleOffMap = offMap.slice(0, remaining);
  const focused = selectedRestaurantId
    ? filtered.find((item) => item.place_id === selectedRestaurantId)
    : null;
  const alreadyVisible = focused
    ? visibleOnMap.some((item) => item.place_id === focused.place_id) ||
      visibleOffMap.some((item) => item.place_id === focused.place_id)
    : false;
  const pinnedFocused = focused && !alreadyVisible ? focused : null;

  return { visibleOnMap, visibleOffMap, pinnedFocused };
}
