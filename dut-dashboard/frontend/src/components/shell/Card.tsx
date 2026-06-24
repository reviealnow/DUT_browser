import { ReactNode } from "react";

type CardProps = {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
};

export function Card({ title, subtitle, actions, className, children }: CardProps) {
  return (
    <section className={`card${className ? ` ${className}` : ""}`}>
      <div className="card-head">
        <div className="card-titles">
          <div className="card-title">{title}</div>
          {subtitle ? <div className="card-sub">{subtitle}</div> : null}
        </div>
        {actions ? <div className="card-actions">{actions}</div> : null}
      </div>
      <div className="card-body">{children}</div>
    </section>
  );
}

type EmptyStateProps = {
  icon?: string;
  message: string;
  hint?: string;
};

export function EmptyState({ icon = "—", message, hint }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <div className="ico" aria-hidden>
        {icon}
      </div>
      <div className="msg">{message}</div>
      {hint ? <div className="hint">{hint}</div> : null}
    </div>
  );
}

type KpiCardProps = {
  label: string;
  value?: ReactNode;
  sub?: string;
};

export function KpiCard({ label, value, sub }: KpiCardProps) {
  const isEmpty = value === undefined || value === null || value === "";
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value${isEmpty ? " empty" : ""}`}>{isEmpty ? "—" : value}</div>
      {sub ? <div className="kpi-sub">{sub}</div> : null}
    </div>
  );
}
