import { useEffect, useState } from "react";
import { formatAge } from "../../utils/datetime";
import LiveDot from "./LiveDot";

/**
 * "● LIVE" in a card's header while the DUT is streaming into that card.
 *
 * Nothing at all when it is not: a card fed by the stream shows its last data
 * after the stream stops, and a dimmed "LIVE" beside it would still read as
 * live at a glance. The word is real text so a screen reader gets the state;
 * the dot beside it is decorative (see LiveDot).
 */
export function LiveBadge({ live }: { live: boolean }) {
  if (!live) {
    return null;
  }
  return (
    <span className="live-badge">
      <LiveDot live />
      Live
    </span>
  );
}

/**
 * Where a card's data is an on-demand capture rather than the stream, say how
 * old it is instead of claiming LIVE. Re-renders every 30s so the age moves
 * while the card sits open; the capture itself is never re-run from here.
 */
export function ScanAge({ capturedAt }: { capturedAt: string | null | undefined }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!capturedAt) {
      return;
    }
    const id = window.setInterval(() => setTick((n) => n + 1), 30_000);
    return () => window.clearInterval(id);
  }, [capturedAt]);
  const age = formatAge(capturedAt ?? null);
  if (!age) {
    return null;
  }
  return <span className="scan-age">Scanned {age}</span>;
}
