import { hashHue } from "../monitoring/authorColor";

/** Colour key: variants like "UI" / "ui" / "usage_insight" vs "Usage Insight"
 * are the same tag server-side, so hash the normalized form for a stable hue. */
function tagHue(name: string): number {
  return hashHue(name.toLowerCase().replace(/[\s_-]/g, ""));
}

/** A coloured tag pill (same pastel formula as author tags). With `onClick`
 * it renders as a button that triggers a workspace tag search. */
export default function TagChip({
  name,
  onClick,
}: {
  name: string;
  onClick?: (name: string) => void;
}) {
  const hue = tagHue(name);
  const style = { background: `hsl(${hue} 65% 86%)`, color: `hsl(${hue} 72% 28%)` };
  if (!onClick) {
    return (
      <span className="tag-chip" style={style}>
        #{name}
      </span>
    );
  }
  return (
    <button
      type="button"
      className="tag-chip tag-chip-btn"
      style={style}
      title={`Search tag "${name}"`}
      onClick={() => onClick(name)}
    >
      #{name}
    </button>
  );
}

/** Row of tag chips; renders nothing for an empty/undefined list. */
export function TagList({
  tags,
  onTagClick,
}: {
  tags?: string[];
  onTagClick?: (name: string) => void;
}) {
  if (!tags || tags.length === 0) return null;
  return (
    <span className="tag-list">
      {tags.map((tag) => (
        <TagChip key={tag} name={tag} onClick={onTagClick} />
      ))}
    </span>
  );
}
