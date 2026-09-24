/**
 * The toolbar's icons, drawn inline.
 *
 * Inline SVG rather than an icon package: the app is offline-first and keeps
 * UI libraries off the render path, and five glyphs do not justify a
 * dependency. Each takes its colour from the text around it and is hidden from
 * assistive tech -- the control it sits in carries the name, in a `.tb-label`
 * that is visible on desktop and screen-reader-only on a phone.
 */

import { ReactNode } from "react";

type IconProps = { size?: number };

function Svg({ size = 18, children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

/** Two stacked units: the DUT registry. */
export function IconDuts(props: IconProps) {
  return (
    <Svg {...props}>
      <rect x="3" y="4" width="18" height="7" rx="2" />
      <rect x="3" y="13" width="18" height="7" rx="2" />
      <path d="M7 7.5h.01M7 16.5h.01" />
    </Svg>
  );
}

/** A plug: open a console on a DUT. */
export function IconConnect(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M9 3v4M15 3v4" />
      <path d="M6 7h12v3a6 6 0 0 1-12 0z" />
      <path d="M12 16v5" />
    </Svg>
  );
}

/** Head and shoulders in a circle: the signed-in account. */
export function IconAccount(props: IconProps) {
  return (
    <Svg {...props}>
      <circle cx="12" cy="12" r="9" />
      <circle cx="12" cy="10" r="3" />
      <path d="M6.2 18.2a7 7 0 0 1 11.6 0" />
    </Svg>
  );
}

/** A door with an arrow leaving it. */
export function IconLogout(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4" />
      <path d="M10 16l-4-4 4-4M6 12h10" />
    </Svg>
  );
}

/** The same door, arrow going in. */
export function IconLogin(props: IconProps) {
  return (
    <Svg {...props}>
      <path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4" />
      <path d="M10 8l4 4-4 4M14 12H4" />
    </Svg>
  );
}
