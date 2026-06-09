export type SectionId =
  | "overview"
  | "cpu"
  | "wifi"
  | "logs"
  | "console"
  | "downloads"
  | "settings";

export type NavItem = {
  id: SectionId;
  label: string;
  icon: string;
  title: string;
  subtitle: string;
};

// Sidebar order mirrors the Luna "Spacing - Dashboards" reference shell.
export const NAV_ITEMS: NavItem[] = [
  { id: "overview", label: "Overview", icon: "▣", title: "Overview", subtitle: "Live DUT monitoring summary" },
  { id: "cpu", label: "CPU / Memory", icon: "📈", title: "CPU / Memory", subtitle: "Per-core CPU and memory trends" },
  { id: "wifi", label: "Wi-Fi Clients", icon: "📶", title: "Wi-Fi Clients", subtitle: "Associated clients by radio" },
  { id: "logs", label: "Logs / Crash Events", icon: "⚠", title: "Logs / Crash Events", subtitle: "Critical crash and log event detection" },
  { id: "console", label: "Serial Console", icon: "⌨", title: "Serial Console", subtitle: "DUT serial / replay console" },
  { id: "downloads", label: "Downloads", icon: "⬇", title: "Downloads", subtitle: "Log bundles and analyzer artifacts" },
  { id: "settings", label: "Settings", icon: "⚙", title: "Settings", subtitle: "Dashboard configuration" },
];
