import type { MachineClient } from "../src/index.ts";

// Compiled with the test suite; this contract is never executed.
export function clientSurface(client: MachineClient): void {
  const closed: Promise<void> = client.close();
  void closed;
  // @ts-expect-error transport shutdown must not bypass owned Run cleanup
  client.closeTransport();
  // @ts-expect-error raw transport requests are not part of the machine API
  client.send("/", {});
}
