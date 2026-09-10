/**
 * A dot that breathes while a session is actually held.
 *
 * The fleet already stated liveness in words — "Connected", "Streaming" — and
 * words are what a screen reader gets and what a reader trusts. What they do
 * not do is catch an eye that is not pointed at the row: a card full of static
 * green text reads identically whether the SSH session is up now or died five
 * minutes ago and nobody re-rendered. The motion is the part that cannot be
 * faked by a stale screenshot, which is the whole reason it is here rather than
 * a brighter colour.
 *
 * Decorative on purpose. The dot is `aria-hidden` and every caller must keep
 * the text beside it: this adds a second channel for the same fact, it does not
 * become the only one. Motion is dropped entirely under
 * `prefers-reduced-motion` (see `.live-dot` in dashboard.css), so the colour
 * still has to carry the state on its own for anyone who set that.
 */
export default function LiveDot({ live }: { live: boolean }) {
  return <span className={`live-dot${live ? " is-live" : ""}`} aria-hidden />;
}
