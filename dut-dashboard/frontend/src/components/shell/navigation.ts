import type { Role } from "../../api/rest";
import { ROLE_RANK } from "../../monitoring/AuthContext";

export type SectionId =
  | "overview"
  | "cpu"
  | "wifi"
  | "ssid"
  | "sitesurvey"
  | "logs"
  | "console"
  | "downloads"
  | "offline"
  | "files"
  | "bulletin"
  | "settings"
  | "firmware";

export type NavGroup = "Monitoring" | "Workspace" | "System";

export type NavItem = {
  id: SectionId;
  label: string;
  icon: string;
  title: string;
  subtitle: string;
  group: NavGroup;
  /** Lowest role that sees this section. Cosmetic — the backend enforces. */
  minRole: Role;
};

// Sidebar order mirrors the Luna "Spacing - Dashboards" reference shell, grouped
// into Monitoring / Workspace / System (see mockup_lanfs_integration.html).
// Role split (P71b): read-only monitoring (incl. the crash feed) is guest;
// anything that drives the DUT, downloads logs or posts content is engineer.
export const NAV_ITEMS: NavItem[] = [
  { id: "overview", label: "Overview", icon: "▣", title: "Overview", subtitle: "Live DUT monitoring summary", group: "Monitoring", minRole: "guest" },
  { id: "cpu", label: "CPU / Memory", icon: "📈", title: "CPU / Memory", subtitle: "Per-core CPU and memory trends", group: "Monitoring", minRole: "guest" },
  { id: "wifi", label: "Wi-Fi Clients", icon: "📶", title: "Wi-Fi Clients", subtitle: "Associated clients by radio", group: "Monitoring", minRole: "guest" },
  { id: "ssid", label: "SSID Capability", icon: "🔍", title: "SSID Capability", subtitle: "DUT config vs host-side scan reconciliation", group: "Monitoring", minRole: "guest" },
  { id: "sitesurvey", label: "Site Survey", icon: "📡", title: "Site Survey", subtitle: "DUT-side neighbor scan and channel recommendation", group: "Monitoring", minRole: "guest" },
  { id: "logs", label: "Logs / Crash Events", icon: "⚠", title: "Logs / Crash Events", subtitle: "Critical crash and log event detection", group: "Monitoring", minRole: "guest" },
  { id: "console", label: "Serial Console", icon: "⌨", title: "Serial Console", subtitle: "DUT serial / replay console", group: "Monitoring", minRole: "engineer" },
  { id: "downloads", label: "Downloads", icon: "⬇", title: "Downloads", subtitle: "Log bundles and analyzer artifacts", group: "Monitoring", minRole: "engineer" },
  { id: "offline", label: "Offline Analyzer", icon: "⌁", title: "Offline Analyzer", subtitle: "Local log comparison and charts", group: "Monitoring", minRole: "guest" },
  { id: "files", label: "Files", icon: "🗂", title: "Files", subtitle: "Shared file workspace", group: "Workspace", minRole: "engineer" },
  { id: "bulletin", label: "Bulletin", icon: "📌", title: "Bulletin", subtitle: "Team notes and replies", group: "Workspace", minRole: "engineer" },
  { id: "settings", label: "Settings", icon: "⚙", title: "Settings", subtitle: "Dashboard configuration", group: "System", minRole: "engineer" },
  { id: "firmware", label: "Upgrade Firmware", icon: "⚡", title: "Upgrade Firmware", subtitle: "Flash a DUT from the Files workspace", group: "System", minRole: "admin" },
];

export function canAccess(item: NavItem, role: Role): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[item.minRole];
}

export function visibleNavItems(role: Role): NavItem[] {
  return NAV_ITEMS.filter((item) => canAccess(item, role));
}
