import { authorColor } from "../monitoring/authorColor";

type Props = {
  name: string | null | undefined;
  /** False when the name was client-supplied text with no session behind it
   * (pre-P71d rows). Undefined means the caller has no verification data, so
   * nothing is claimed either way. */
  verified?: boolean;
};

/** A coloured name pill — same author always gets the same colour (see authorColor.ts).
 * An unverified name is marked rather than silently shown as if it were checked. */
export default function AuthorTag({ name, verified }: Props) {
  const label = (name ?? "").trim() || "—";
  const { bg, fg } = authorColor(name);
  return (
    <span className="author-tag-wrap">
      <span className="author-tag" style={{ background: bg, color: fg }}>
        {label}
      </span>
      {verified === false && label !== "—" ? (
        <span className="author-unverified" title="Posted before names were tied to a login — not verified">
          unverified
        </span>
      ) : null}
    </span>
  );
}
