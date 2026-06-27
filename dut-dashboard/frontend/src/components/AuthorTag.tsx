import { authorColor } from "../monitoring/authorColor";

/** A coloured name pill — same author always gets the same colour (see authorColor.ts). */
export default function AuthorTag({ name }: { name: string | null | undefined }) {
  const label = (name ?? "").trim() || "—";
  const { bg, fg } = authorColor(name);
  return (
    <span className="author-tag" style={{ background: bg, color: fg }}>
      {label}
    </span>
  );
}
