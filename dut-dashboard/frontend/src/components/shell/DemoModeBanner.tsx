/**
 * Says, on every screen of the Demo Mode build, that nothing here is a real
 * DUT. The public page must never be mistaken for a bench connection, so this
 * is not dismissable. Styled as the update banner so it reads as app chrome.
 */
export default function DemoModeBanner() {
  return (
    <div className="update-banner demo-banner" role="status" aria-label="Demo Mode">
      <span className="pill warn">
        <span className="dot" />
        DEMO MODE
      </span>
      <span className="update-banner-text">
        Replaying synthetic DUT telemetry — no device, serial port or backend is connected. Actions that need a real
        DUT are disabled. Run the dashboard locally for live monitoring.
      </span>
    </div>
  );
}
