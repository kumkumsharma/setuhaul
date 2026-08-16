/** Format Before/After rates (0–1 fractions) as whole-number percentages. */

export function formatRateAsPercent(rate) {
  if (rate == null || rate === "") return "—";
  const n = Number(rate);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(0)}%`;
}
