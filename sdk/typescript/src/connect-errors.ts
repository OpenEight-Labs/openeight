// Decode only the bounded public google.rpc.ErrorInfo contract.
import { errorDetails } from "./error-catalog.ts";

export function governanceReason(
  status: number,
  code: string,
  details: unknown,
): string | undefined {
  if (!Array.isArray(details) || details.length > 8) return;
  const infos = details.filter(
    (d) => d && typeof d === "object" && d.type === "google.rpc.ErrorInfo",
  );
  if (infos.length !== 1) return;
  const encoded = infos[0].value;
  if (
    typeof encoded !== "string" ||
    encoded.length > 1024 ||
    !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)
  )
    return;
  try {
    const raw = Uint8Array.from(atob(encoded), (c) => c.charCodeAt(0));
    let pos = 0;
    const varint = () => {
      let value = 0;
      for (let shift = 0; shift < 35; shift += 7) {
        if (pos >= raw.length) throw new Error();
        const byte = raw[pos++]!;
        value += (byte & 127) * 2 ** shift;
        if (byte < 128) return value;
      }
      throw new Error();
    };
    const fields = new Map<number, string>();
    while (pos < raw.length) {
      const tag = varint();
      if (tag !== 10 && tag !== 18 && tag !== 26) return;
      const length = varint(),
        end = pos + length;
      if (end > raw.length) return;
      if (tag !== 26) {
        if (fields.has(tag)) return;
        fields.set(
          tag,
          new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
            raw.subarray(pos, end),
          ),
        );
      }
      pos = end;
    }
    const reason = fields.get(10),
      expected = reason === undefined ? undefined : errorDetails[reason];
    if (
      expected &&
      fields.get(18) === expected[0] &&
      status === expected[1] &&
      code === expected[2]
    )
      return reason;
  } catch {
    /* Malformed details retain only the protocol error. */
  }
}
