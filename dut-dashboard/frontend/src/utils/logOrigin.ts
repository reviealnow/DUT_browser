import { LogOrigin } from "../api/rest";

/**
 * One line saying which unit, which DUT and which host a session log came
 * from, or null when the log states none of it.
 *
 * The unit leads because it is the one thing a filename cannot carry: it is
 * only known once the device answers, after the file already exists. A model
 * without a unit says so, rather than reading as an identity -- two AP6_420Es
 * share a model.
 */
export function describeLogOrigin(origin: LogOrigin | undefined): string | null {
  if (!origin) return null;
  const parts: string[] = [];

  if (origin.device_id) parts.push(origin.device_id);
  else if (origin.model) parts.push(`${origin.model} (unit not identified)`);

  const dut = origin.label ?? origin.dut_id;
  if (dut) parts.push(`DUT ${dut}`);

  if (origin.host) {
    const collector = origin.collector_id ? ` [${origin.collector_id}]` : "";
    parts.push(`via ${origin.host}${collector}${origin.source ? ` ${origin.source}` : ""}`);
  } else if (origin.mode === "serial" && origin.source) {
    parts.push(`cable ${origin.source}`);
  } else if (origin.mode === "replay") {
    parts.push("replay");
  }

  return parts.length > 0 ? parts.join(" · ") : null;
}
