import { ReactNode } from "react";

import { ROLE_RANK, useAuth } from "../monitoring/AuthContext";
import { FleetEntry } from "../monitoring/useFleetMonitor";

/**
 * The line above a collapsed list of DUT cards: how many there are, how many it
 * is showing, and the way back.
 *
 * Two pages collapse a list of DUTs for different reasons -- the Fleet grid
 * because the device answered that it is in no mesh, the Overview strip because
 * a registration is kept until somebody removes it -- and everything except
 * that reason is the same sentence. It was the same sentence twice until this
 * existed, which is the shape this repository loses things in: the next
 * correction lands in one copy.
 *
 * So the reason is the prop and the rest lives here. What is shared is not
 * decoration: the count is the reader's cross-check that the page is filtering
 * rather than losing DUTs, and the toggle is the way back. A hidden card with
 * neither is indistinguishable from the app having dropped the device.
 */
export default function FleetCollapseNotice({
  fleet,
  consoleOpen,
  shown,
  collapsed,
  showAll,
  onShowAll,
  lead,
  children,
}: {
  /** Every registered DUT — what the count counts. */
  fleet: FleetEntry[];
  /** Those with a console open — what a collapsed list collapses to. */
  consoleOpen: FleetEntry[];
  /** What the caller is actually drawing right now. */
  shown: FleetEntry[];
  /** Whether collapsing is in effect at all. Not derivable from `shown`:
   *  with `showAll` on, nothing is hidden and the notice must still offer the
   *  way back. */
  collapsed: boolean;
  showAll: boolean;
  onShowAll: (next: boolean) => void;
  /** The half-sentence the two callers do not share, ending in the thing being
   *  filtered so the clause below completes it — "The DUT reports no mesh, so
   *  the grid" → "… so the grid shows the DUTs on a console and hides 2
   *  others". */
  lead: string;
  /** Anything the caller wants after the sentence, e.g. the Fleet page's note
   *  that backhaul figures do not refresh on their own. */
  children?: ReactNode;
}) {
  const { role } = useAuth();
  const hidden = fleet.filter((entry) => !shown.includes(entry));
  // Whether this reader would have seen a Connect button on any of the hidden
  // cards, by the same rule the card itself applies -- admin to drive a remote
  // node, engineer for one cabled here. It matters because the card is the only
  // place a remembered DUT can be reconnected from, so collapsing the list also
  // collapses the way back and the sentence has to say so.
  //
  // Asked per hidden entry rather than assumed, because a guest sees no Connect
  // button at all and telling them their Connect buttons were hidden would send
  // them looking for something that was never on the page.
  const hidesConnect = hidden.some(
    (entry) => ROLE_RANK[role] >= ROLE_RANK[entry.remote ? "admin" : "engineer"],
  );

  return (
    <div className="setting-hint">
      {/* Said where the count is, because the count is what stops adding up
          otherwise: a reader who sees "3 registered" above one card needs the
          reason in the same breath, not two paragraphs away. */}
      <strong className="fleet-count">
        {fleet.length} registered · {consoleOpen.length} with a console open.
      </strong>{" "}
      {collapsed && !showAll ? (
        <>
          {lead} shows the {consoleOpen.length === 1 ? "DUT" : "DUTs"} on a console and{" "}
          {hidden.length === 1 ? "hides 1 other" : `hides ${hidden.length} others`}
          {hidesConnect
            ? hidden.length === 1
              ? " — with its Connect button"
              : " — with their Connect buttons"
            : ""}
          .{" "}
          <button type="button" className="linklike" onClick={() => onShowAll(true)}>
            Show all
          </button>
          .{" "}
        </>
      ) : null}
      {collapsed && showAll ? (
        <>
          Showing every registered DUT.{" "}
          {/* Plural, unlike the singular this used to carry on the Fleet page:
              "the console" is not necessarily one, and that page's own tests
              cover two being open at once. */}
          <button type="button" className="linklike" onClick={() => onShowAll(false)}>
            Show only open consoles
          </button>
          .{" "}
        </>
      ) : null}
      {children}
    </div>
  );
}
