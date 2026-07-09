export type SectionId =
  | "overview"
  | "cpu"
  | "wifi"
  | "ssid"
  | "sitesurvey"
  | "logs"
  | "console"
  | "downloads"
  | "files"
  | "bulletin"
  | "settings";

export type NavGroup = "Monitoring" | "Workspace" | "System";

export type NavItem = {
  id: SectionId;
  label: string;
  icon: string;
  title: string;
  subtitle: string;
  group: NavGroup;
};

// Sidebar order mirrors the Luna "Spacing - Dashboards" reference shell, grouped
// into Monitoring / Workspace / System (see mockup_lanfs_integration.html).
export const NAV_ITEMS: NavItem[] = [
  { id: "overview", label: "Overview", icon: "▣", title: "Overview", subtitle: "Live DUT monitoring summary", group: "Monitoring" },
  { id: "cpu", label: "CPU / Memory", icon: "📈", title: "CPU / Memory", subtitle: "Per-core CPU and memory trends", group: "Monitoring" },
  { id: "wifi", label: "Wi-Fi Clients", icon: "📶", title: "Wi-Fi Clients", subtitle: "Associated clients by radio", group: "Monitoring" },
  { id: "ssid", label: "SSID Capability", icon: "🔍", title: "SSID Capability", subtitle: "DUT config vs host-side scan reconciliation", group: "Monitoring" },
  { id: "sitesurvey", label: "Site Survey", icon: "📡", title: "Site Survey", subtitle: "DUT-side neighbor scan and channel recommendation", group: "Monitoring" },
  { id: "logs", label: "Logs / Crash Events", icon: "⚠", title: "Logs / Crash Events", subtitle: "Critical crash and log event detection", group: "Monitoring" },
  { id: "console", label: "Serial Console", icon: "⌨", title: "Serial Console", subtitle: "DUT serial / replay console", group: "Monitoring" },
  { id: "downloads", label: "Downloads", icon: "⬇", title: "Downloads", subtitle: "Log bundles and analyzer artifacts", group: "Monitoring" },
  { id: "files", label: "Files", icon: "🗂", title: "Files", subtitle: "Shared file workspace", group: "Workspace" },
  { id: "bulletin", label: "Bulletin", icon: "📌", title: "Bulletin", subtitle: "Team notes and replies", group: "Workspace" },
  { id: "settings", label: "Settings", icon: "⚙", title: "Settings", subtitle: "Dashboard configuration", group: "System" },
];
