/** Copy text to the clipboard, including on plain HTTP LAN origins where the
 * Clipboard API is unavailable. Restores focus after the fallback selection. */
export async function copyToClipboard(text: string): Promise<boolean> {
  const previouslyFocused =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;

  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch {
        // Fall through to the selection-based path.
      }
    }

    const holder = document.createElement("textarea");
    holder.value = text;
    holder.setAttribute("readonly", "");
    holder.style.position = "fixed";
    holder.style.opacity = "0";
    document.body.appendChild(holder);
    try {
      holder.select();
      return document.execCommand("copy");
    } finally {
      holder.remove();
    }
  } catch {
    return false;
  } finally {
    if (previouslyFocused?.isConnected) {
      previouslyFocused.focus({ preventScroll: true });
    }
  }
}
