import { useEffect, useState } from "react";

import {
  getFileDownloadUrl,
  searchWorkspace,
  WorkspaceSearchResult,
} from "../api/rest";
import AuthorTag from "./AuthorTag";
import { TagList } from "./TagChip";
import { Card, EmptyState } from "./shell/Card";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Combined fuzzy tag-search results: the files table and the bulletin list
 * on one panel. Opened by submitting the workspace search box or clicking a
 * tag chip; Close returns to the section that was active. */
export default function WorkspaceSearchResults({
  query,
  onTagClick,
  onClose,
}: {
  query: string;
  onTagClick: (tag: string) => void;
  onClose: () => void;
}) {
  const [result, setResult] = useState<WorkspaceSearchResult | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setResult(null);
    setFailed(false);
    searchWorkspace(query)
      .then((r) => !cancelled && setResult(r))
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, [query]);

  const closeBtn = (
    <button type="button" className="btn" onClick={onClose}>
      ✕ Close
    </button>
  );

  if (failed) {
    return (
      <Card title="Tag search" subtitle={`Results for "${query}"`} actions={closeBtn}>
        <EmptyState icon="🏷" message="Search failed" hint="Is the backend reachable?" />
      </Card>
    );
  }
  if (result === null) {
    return (
      <Card title="Tag search" subtitle={`Results for "${query}"`} actions={closeBtn}>
        <EmptyState icon="🏷" message="Searching…" />
      </Card>
    );
  }

  const empty = result.files.length === 0 && result.posts.length === 0;
  return (
    <Card
      title="Tag search"
      subtitle={
        <>
          Results for "{query}" ·{" "}
          {result.matched_tags.length === 0 ? (
            "no matching tags"
          ) : (
            <TagList tags={result.matched_tags.map((t) => t.name)} onTagClick={onTagClick} />
          )}
        </>
      }
      actions={closeBtn}
    >
      {empty ? (
        <EmptyState
          icon="🏷"
          message="No tagged items match"
          hint='Fuzzy match covers abbreviations too — "ui" finds "usage_insight".'
        />
      ) : (
        <div style={{ display: "grid", gap: "var(--space-4)" }}>
          {result.files.length > 0 ? (
            <div>
              <h4 className="search-group-title">Files ({result.files.length})</h4>
              <div className="logscroll">
                <table className="filetable">
                  <thead>
                    <tr>
                      <th>Filename</th>
                      <th>Size</th>
                      <th>Uploader</th>
                      <th aria-label="actions" />
                    </tr>
                  </thead>
                  <tbody>
                    {result.files.map((file) => (
                      <tr key={file.id}>
                        <td className="filetable-name">
                          {file.filename} <TagList tags={file.tags} onTagClick={onTagClick} />
                        </td>
                        <td>{formatSize(file.size)}</td>
                        <td>
                          <AuthorTag name={file.uploader} verified={file.uploader_verified} />
                        </td>
                        <td className="filetable-actions">
                          <div className="row-actions">
                            <a
                              className="icon-btn"
                              href={getFileDownloadUrl(file.id)}
                              download={file.filename}
                              title="Download"
                              aria-label={`Download ${file.filename}`}
                            >
                              ↓
                            </a>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}
          {result.posts.length > 0 ? (
            <div>
              <h4 className="search-group-title">Bulletin ({result.posts.length})</h4>
              {result.posts.map((post) => (
                <div className="note" key={post.id}>
                  <h4>
                    {post.title} <TagList tags={post.tags} onTagClick={onTagClick} />
                  </h4>
                  <p>{post.body}</p>
                  <div className="meta">
                    <AuthorTag name={post.author} verified={post.author_verified} /> · {post.created_at.replace("T", " ")}
                  </div>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      )}
    </Card>
  );
}
