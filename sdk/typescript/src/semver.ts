const grammar =
  /^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$/;
export function isSemVer(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 128) return false;
  const m = grammar.exec(value);
  if (!m || m[0] !== value) return false;
  const bounded = (s: string) => BigInt(s) <= 18446744073709551615n;
  if (!m.slice(1, 4).every(bounded)) return false;
  return (
    !m[4] ||
    m[4]
      .split(".")
      .every((s) => !/^\d+$/.test(s) || (!/^0\d/.test(s) && bounded(s)))
  );
}
