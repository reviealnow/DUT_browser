import { CSSProperties, FormEvent, KeyboardEvent as ReactKeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { keymap } from "@codemirror/view";
import CodeMirror, { EditorView, ViewUpdate } from "@uiw/react-codemirror";
import { vim } from "@replit/codemirror-vim";

type Props = {
  lines: string[];
  /**
   * Position of `lines[0]` in the whole session's stream, from `useDutMonitor`.
   * `linesStartSeq + index` is what each rendered line is keyed by, and it has
   * to come from outside: the window slides, so nothing inside this component
   * can tell "twenty new lines" from "the same lines, moved up twenty".
   */
  linesStartSeq: number;
  onSend: (text: string) => Promise<void>;
  onDownloadLog: () => void;
  canDownloadLog: boolean;
};

export default function ConsolePanel({
  lines,
  linesStartSeq,
  onSend,
  onDownloadLog,
  canDownloadLog,
}: Props) {
  const LONG_COMMAND_WORD_THRESHOLD = 100;
  const [command, setCommand] = useState("");
  const [draftCommand, setDraftCommand] = useState("");
  const [isEditorOpen, setIsEditorOpen] = useState(false);
  const consoleRef = useRef<HTMLDivElement | null>(null);
  const editorViewRef = useRef<EditorView | null>(null);
  const draftCommandRef = useRef("");
  const [stickToBottom, setStickToBottom] = useState(true);

  const commandWordCount = countWords(command);
  const draftWordCount = countWords(draftCommand);
  const shouldSuggestPopup = commandWordCount > LONG_COMMAND_WORD_THRESHOLD || command.includes("\n");

  function scrollToBottom() {
    if (!consoleRef.current) {
      return;
    }
    consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
  }

  function handleConsoleScroll() {
    if (!consoleRef.current) {
      return;
    }
    const { scrollTop, scrollHeight, clientHeight } = consoleRef.current;
    const isNearBottom = scrollHeight - (scrollTop + clientHeight) < 20;
    setStickToBottom(isNearBottom);
  }

  useEffect(() => {
    if (stickToBottom) {
      scrollToBottom();
    }
  }, [lines, command, stickToBottom]);

  useEffect(() => {
    draftCommandRef.current = draftCommand;
  }, [draftCommand]);

  useEffect(() => {
    if (!isEditorOpen || !editorViewRef.current) {
      return;
    }
    editorViewRef.current.focus();
  }, [isEditorOpen]);

  useEffect(() => {
    if (!isEditorOpen) {
      return;
    }
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = originalOverflow;
    };
  }, [isEditorOpen]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    await sendCommand(command, setCommand);
  }

  function handleInputKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Tab") {
      return;
    }
    event.preventDefault();
    void onSend("\t");
  }

  useEffect(() => {
    function hasActiveTextSelection(target: EventTarget | null): boolean {
      const selectionText = window.getSelection()?.toString() ?? "";
      if (selectionText.length > 0) {
        return true;
      }
      if (!(target instanceof HTMLElement)) {
        return false;
      }
      if (target.isContentEditable) {
        return true;
      }
      if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
        const start = target.selectionStart ?? 0;
        const end = target.selectionEnd ?? 0;
        return start !== end;
      }
      return false;
    }

    function handleGlobalKeyDown(event: KeyboardEvent) {
      const isCtrlC = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "c";
      if (!isCtrlC || event.altKey || event.shiftKey) {
        return;
      }
      if (hasActiveTextSelection(event.target)) {
        return;
      }
      event.preventDefault();
      void onSend("\u0003");
    }

    window.addEventListener("keydown", handleGlobalKeyDown, { capture: true });
    return () => {
      window.removeEventListener("keydown", handleGlobalKeyDown, { capture: true });
    };
  }, [onSend]);

  function openEditor() {
    setDraftCommand(command);
    setIsEditorOpen(true);
  }

  function closeEditor() {
    setIsEditorOpen(false);
  }

  async function sendCommand(value: string, reset: (next: string) => void) {
    if (!value.trim()) {
      return;
    }
    const payload = value.endsWith("\n") ? value : `${value}\n`;
    await onSend(payload);
    reset("");
  }

  async function handlePopupSend() {
    await sendCommand(draftCommand, (next) => {
      setDraftCommand(next);
      setCommand(next);
      setIsEditorOpen(false);
    });
  }

  const editorExtensions = useMemo(
    () => [
      vim({ status: true }),
      keymap.of([
        {
          key: "Ctrl-Enter",
          run: () => {
            void sendCommand(draftCommandRef.current, (next) => {
              setDraftCommand(next);
              setCommand(next);
              setIsEditorOpen(false);
            });
            return true;
          },
        },
        {
          key: "Mod-Enter",
          run: () => {
            void sendCommand(draftCommandRef.current, (next) => {
              setDraftCommand(next);
              setCommand(next);
              setIsEditorOpen(false);
            });
            return true;
          },
        },
      ]),
      EditorView.lineWrapping,
      EditorView.theme({
        "&": {
          fontSize: "13px",
          border: "1px solid #bbb",
          borderRadius: "8px",
          overflow: "hidden",
        },
        ".cm-scroller": {
          fontFamily: "monospace",
          minHeight: "320px",
          maxHeight: "55vh",
          overflow: "auto",
        },
        ".cm-content": {
          padding: "12px",
          lineHeight: "1.5",
        },
        ".cm-focused": {
          outline: "none",
        },
        ".cm-vim-panel": {
          borderTop: "1px solid #ddd",
          padding: "6px 10px",
          fontFamily: "monospace",
          fontSize: "12px",
          background: "#f6f6f6",
          color: "#444",
        },
      }),
    ],
    [],
  );

  function handleEditorChange(value: string, _viewUpdate: ViewUpdate) {
    setDraftCommand(value);
  }

  return (
    <div style={{ border: "1px solid #ddd", padding: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>Serial Console</h3>
        <div style={{ display: "flex", gap: 8 }}>
          <button type="button" onClick={onDownloadLog} disabled={!canDownloadLog}>
            Download DUT Log
          </button>
        </div>
      </div>
      <div
        ref={consoleRef}
        onScroll={handleConsoleScroll}
        style={{
          height: 320,
          overflowY: "auto",
          background: "#121212",
          color: "#f5f5f5",
          fontFamily: "monospace",
          fontSize: 12,
          padding: 8,
          whiteSpace: "pre-wrap",
        }}
      >
        {/* One span per line, each carrying its own newline, keyed by position
            in the stream.

            It was one text node holding `lines.join("\n")`, so every batch
            rebuilt and re-laid out all 183 KB of it: 6.46 ms an update,
            measured in Chrome on 1000 lines of real sysMon output. Under a site
            survey -- tens of thousands of `iw scan` lines, 39,052 in one session
            on this bench -- that is most of the main thread, and the page stops
            answering the mouse. Per line, React touches only what arrived or
            left: 2.25 ms.

            Two things about the shape are load-bearing, and both were arrived at
            by measuring rather than by reasoning:

            INLINE, not block. Block elements are faster still (0.50 ms) and they
            break the console as a thing you can copy out of: an empty <div> is
            zero-high AND contributes nothing to a selection, so every blank line
            the DUT printed vanishes from what the operator pastes into a bug
            report. Inline spans holding their own "\n" serialise exactly as the
            joined text node did -- verified character for character, the only
            difference being one trailing newline at the very end. They also
            leave `line-height` alone at 18px; the block version needed it
            overridden to give blank lines height, which visibly tightened the
            console.

            The KEY is the stream position, never the array index. The window
            slides, so index N names a different line after every batch and React
            rewrites all thousand nodes: 7.44 ms, worse than the single node this
            replaced. */}
        {lines.map((line, index) => (
          <span key={linesStartSeq + index}>{line + "\n"}</span>
        ))}
      </div>
      <form onSubmit={handleSubmit} style={{ marginTop: 8, display: "flex", gap: 8 }}>
        <input
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          onKeyDown={handleInputKeyDown}
          placeholder="Type command"
          style={{ flex: 1 }}
        />
        <button type="button" onClick={openEditor}>
          Edit in Popup
        </button>
        <button type="submit">Send</button>
      </form>
      <div style={{ marginTop: 6, fontSize: 12, color: shouldSuggestPopup ? "#8a4b00" : "#666" }}>
        {shouldSuggestPopup
          ? `Long input detected (${commandWordCount} words). The popup editor is better for scripts and multiline text.`
          : "Use the popup editor for scripts, multiline commands, or large pasted content."}
      </div>
      {isEditorOpen ? (
        <div style={overlayStyle} role="dialog" aria-modal="true" aria-labelledby="command-editor-title">
          <div style={modalStyle}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
              <div>
                <h3 id="command-editor-title" style={{ margin: 0 }}>
                  Command Editor
                </h3>
                <div style={{ marginTop: 4, fontSize: 12, color: "#555" }}>
                  Paste or edit longer scripts here. Vim mode is enabled. `Ctrl+Enter` sends.
                </div>
              </div>
              <button type="button" onClick={closeEditor} aria-label="Close command editor">
                Close
              </button>
            </div>
            <CodeMirror
              value={draftCommand}
              onChange={handleEditorChange}
              onCreateEditor={(view) => {
                editorViewRef.current = view;
              }}
              basicSetup={{
                lineNumbers: true,
                foldGutter: false,
              }}
              extensions={editorExtensions}
              theme="light"
              indentWithTab
              autoFocus
              placeholder="Paste commands or script text"
              style={editorWrapperStyle}
            />
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
              <div style={{ fontSize: 12, color: draftWordCount > LONG_COMMAND_WORD_THRESHOLD ? "#8a4b00" : "#555" }}>
                {draftWordCount} words, {draftCommand.split("\n").length} lines
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  onClick={() => {
                    setCommand(draftCommand);
                    closeEditor();
                  }}
                >
                  Keep Draft
                </button>
                <button type="button" onClick={() => void handlePopupSend()}>
                  Send
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function countWords(value: string): number {
  const trimmed = value.trim();
  if (!trimmed) {
    return 0;
  }
  return trimmed.split(/\s+/).length;
}

const overlayStyle: CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(18, 18, 18, 0.55)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  padding: 16,
  zIndex: 1000,
};

const modalStyle: CSSProperties = {
  width: "min(900px, 100%)",
  maxHeight: "min(80vh, 720px)",
  background: "#fff",
  borderRadius: 10,
  boxShadow: "0 18px 48px rgba(0, 0, 0, 0.25)",
  padding: 16,
  display: "grid",
  gap: 12,
};

const editorWrapperStyle: CSSProperties = {
  width: "100%",
};
