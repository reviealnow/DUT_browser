import { useEffect, useId, useState } from "react";

import { getWorkspaceTags } from "../api/rest";

/** Comma-separated tag text -> clean tag-name list (trimmed, no empties). */
export function parseTags(text: string): string[] {
  return text
    .split(",")
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
}

/** Controlled comma-separated tag input with datalist suggestions from the
 * existing workspace tags. Parents keep the raw text and parseTags() it on
 * submit, so typing "a, b" mid-edit never fights the caret. */
export default function TagInput({
  value,
  onChange,
  ariaLabel = "Tags",
}: {
  value: string;
  onChange: (text: string) => void;
  ariaLabel?: string;
}) {
  const listId = useId();
  const [suggestions, setSuggestions] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    getWorkspaceTags()
      .then((tags) => !cancelled && setSuggestions(tags.map((t) => t.name)))
      .catch(() => undefined); // suggestions are best-effort
    return () => {
      cancelled = true;
    };
  }, []);

  // The datalist suggests a completion for the tag currently being typed
  // (the text after the last comma), keeping what's already entered.
  const committed = value.includes(",") ? value.slice(0, value.lastIndexOf(",") + 1) : "";
  return (
    <>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Tags (comma-separated, optional)"
        aria-label={ariaLabel}
        maxLength={200}
        list={listId}
      />
      <datalist id={listId}>
        {suggestions.map((name) => (
          <option key={name} value={`${committed}${committed ? " " : ""}${name}`}>
            {name}
          </option>
        ))}
      </datalist>
    </>
  );
}
