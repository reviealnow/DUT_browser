/**
 * The single DUT id used today. The backend keys all per-DUT runtime state by
 * this id (see app/dut/registry.py). A0 keeps exactly one DUT and no switcher;
 * threading this constant through the API/WS calls makes the wire multi-DUT
 * ready so a later phase only needs to add a UI selector.
 */
export const DEFAULT_DUT_ID = "default";
