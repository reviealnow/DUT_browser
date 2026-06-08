import { ReactNode } from "react";

type Props = {
  title: string;
  subtitle: string;
  /** Optional search slot — only render where the current data supports filtering. */
  search?: ReactNode;
  /** Right-aligned status pills + primary action. */
  actions?: ReactNode;
};

export default function Topbar({ title, subtitle, search, actions }: Props) {
  return (
    <header className="toolbar">
      <div className="toolbar-titles">
        <div className="toolbar-title">{title}</div>
        <div className="toolbar-sub">{subtitle}</div>
      </div>
      <div className="toolbar-spacer" />
      {search}
      {actions}
    </header>
  );
}
