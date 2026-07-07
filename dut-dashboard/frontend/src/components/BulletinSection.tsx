import { useCallback, useEffect, useRef, useState } from "react";

import {
  BulletinComment,
  BulletinPost,
  createBulletinComment,
  createBulletinPost,
  deleteBulletinPost,
  getBulletinPosts,
  humanizeApiError,
} from "../api/rest";
import { useIdentity } from "../monitoring/useSettings";
import AuthorTag from "./AuthorTag";
import { Card, EmptyState } from "./shell/Card";

type Identity = ReturnType<typeof useIdentity>;

function formatTime(iso: string): string {
  return iso.replace("T", " ");
}

function countReplies(comments: BulletinComment[]): number {
  return comments.reduce((total, c) => total + 1 + countReplies(c.replies), 0);
}

/** Shared, editable "Posting as <name>" identity field (IP-default, persisted). */
function PostingAs({ identity }: { identity: Identity }) {
  return (
    <label className="posting-as">
      <span>Posting as</span>
      <input
        type="text"
        value={identity.displayName}
        onChange={(e) => identity.setDisplayName(e.target.value)}
        placeholder={identity.suggested || "your name"}
        aria-label="Your display name"
        maxLength={40}
      />
      {identity.effectiveName ? <AuthorTag name={identity.effectiveName} /> : null}
    </label>
  );
}

function NewPostCard({ onPosted, identity }: { onPosted: () => void; identity: Identity }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await createBulletinPost(title, body, identity.effectiveName || null);
      setTitle("");
      setBody("");
      onPosted();
    } catch (e) {
      setError(humanizeApiError(e));
    } finally {
      setBusy(false);
    }
  }, [title, body, identity.effectiveName, onPosted]);

  const canPost =
    title.trim().length > 0 && body.trim().length > 0 && identity.effectiveName.length > 0 && !busy;

  return (
    <Card title="New note" subtitle="Pin a note to the board">
      <div style={{ display: "grid", gap: "var(--space-3)" }}>
        <PostingAs identity={identity} />
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Title"
          aria-label="Post title"
          maxLength={120}
        />
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder="What's the note?"
          aria-label="Post content"
          maxLength={1000}
          rows={3}
        />
        {error ? <div className="flash" style={{ color: "var(--danger)" }}>{error}</div> : null}
        <div>
          <button type="button" className="btn primary" disabled={!canPost} onClick={() => void submit()}>
            {busy ? "Posting…" : "Post note"}
          </button>
        </div>
      </div>
    </Card>
  );
}

function ReplyBox({
  postId,
  parentId,
  onReplied,
  identity,
}: {
  postId: number;
  parentId?: number;
  onReplied: () => void;
  identity: Identity;
}) {
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canReply = body.trim().length > 0 && identity.effectiveName.length > 0 && !busy;

  const submit = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await createBulletinComment(postId, body, identity.effectiveName || null, parentId ?? null);
      setBody("");
      onReplied();
    } catch (e) {
      setError(humanizeApiError(e));
    } finally {
      setBusy(false);
    }
  }, [postId, parentId, body, identity.effectiveName, onReplied]);

  return (
    <div style={{ display: "grid", gap: "var(--space-2)", marginTop: "var(--space-2)" }}>
      <PostingAs identity={identity} />
      <div style={{ display: "flex", gap: "var(--space-2)" }}>
        <input
          type="text"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && canReply && void submit()}
          placeholder={parentId ? "Reply…" : "Add a reply…"}
          aria-label="Reply"
          maxLength={600}
          style={{ flex: 1 }}
        />
        <button type="button" className="btn" disabled={!canReply} onClick={() => void submit()}>
          {busy ? "…" : "Reply"}
        </button>
        {error ? <span className="flash" style={{ color: "var(--danger)" }}>{error}</span> : null}
      </div>
    </div>
  );
}

function CommentThread({
  comment,
  postId,
  onReplied,
  identity,
}: {
  comment: BulletinComment;
  postId: number;
  onReplied: () => void;
  identity: Identity;
}) {
  const [replying, setReplying] = useState(false);
  return (
    <div className="note" style={{ marginBottom: "var(--space-2)" }}>
      <p>{comment.body}</p>
      <div className="meta">
        <AuthorTag name={comment.author} /> · {formatTime(comment.created_at)} ·{" "}
        <button type="button" className="linklike" onClick={() => setReplying((v) => !v)}>
          Reply
        </button>
      </div>
      {replying ? (
        <ReplyBox
          postId={postId}
          parentId={comment.id}
          identity={identity}
          onReplied={() => {
            setReplying(false);
            onReplied();
          }}
        />
      ) : null}
      {comment.replies.length > 0 ? (
        <div style={{ marginTop: "var(--space-2)", paddingLeft: "var(--space-4)" }}>
          {comment.replies.map((reply) => (
            <div className="note" key={reply.id} style={{ marginBottom: "var(--space-2)" }}>
              <p>{reply.body}</p>
              <div className="meta">
                <AuthorTag name={reply.author} /> · {formatTime(reply.created_at)}
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function PostCard({ post, onChanged, identity }: { post: BulletinPost; onChanged: () => void; identity: Identity }) {
  const [open, setOpen] = useState(false);
  const replies = countReplies(post.comments);

  const onDelete = useCallback(() => {
    if (!window.confirm(`Delete "${post.title}" and its replies? This cannot be undone.`)) {
      return;
    }
    deleteBulletinPost(post.id)
      .then(onChanged)
      .catch(() => onChanged());
  }, [post.id, post.title, onChanged]);

  return (
    <Card
      title={post.title}
      subtitle={
        <>
          <AuthorTag name={post.author} /> · {formatTime(post.created_at)} · {replies}{" "}
          {replies === 1 ? "reply" : "replies"}
        </>
      }
      actions={
        <div className="row-actions">
          <button type="button" className="btn" onClick={() => setOpen((v) => !v)}>
            {open ? "Hide" : "Open"}
          </button>
          <button
            type="button"
            className="icon-btn danger"
            title="Delete note"
            aria-label={`Delete ${post.title}`}
            onClick={onDelete}
          >
            🗑
          </button>
        </div>
      }
    >
      <p style={{ margin: 0, color: "var(--ink)" }}>{post.body}</p>
      {open ? (
        <div style={{ marginTop: "var(--space-3)" }}>
          {post.comments.map((comment) => (
            <CommentThread key={comment.id} comment={comment} postId={post.id} onReplied={onChanged} identity={identity} />
          ))}
          <ReplyBox postId={post.id} onReplied={onChanged} identity={identity} />
        </div>
      ) : null}
    </Card>
  );
}

// First page size; "Load more" appends another page (the board accumulates
// notes over months, so the view lazy-loads instead of fetching every post).
const PAGE_SIZE = 20;

export default function BulletinSection({ query = "" }: { query?: string }) {
  const [posts, setPosts] = useState<BulletinPost[] | null>(null);
  const [total, setTotal] = useState(0);
  const [failed, setFailed] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const identity = useIdentity();
  // Posts currently on screen: reload refetches that many from offset 0 so a
  // post/reply/delete refresh doesn't collapse the board back to the first page.
  const loadedRef = useRef(PAGE_SIZE);

  const reload = useCallback(() => {
    setFailed(false);
    getBulletinPosts(Math.max(PAGE_SIZE, loadedRef.current), 0)
      .then((page) => {
        loadedRef.current = page.posts.length;
        setPosts(page.posts);
        setTotal(page.total);
      })
      .catch(() => setFailed(true));
  }, []);

  const loadMore = useCallback(() => {
    if (posts === null) return;
    setLoadingMore(true);
    getBulletinPosts(PAGE_SIZE, posts.length)
      .then((page) => {
        setPosts((prev) => {
          const current = prev ?? [];
          // Dedupe on id: a post landing between page fetches shifts offsets.
          const known = new Set(current.map((p) => p.id));
          const merged = [...current, ...page.posts.filter((p) => !known.has(p.id))];
          loadedRef.current = merged.length;
          return merged;
        });
        setTotal(page.total);
      })
      .catch(() => undefined) // keep the loaded posts; the button stays for retry
      .finally(() => setLoadingMore(false));
  }, [posts]);

  useEffect(() => {
    reload();
  }, [reload]);

  const needle = query.trim().toLowerCase();
  const visible = (posts ?? []).filter(
    (p) => !needle || p.title.toLowerCase().includes(needle) || p.body.toLowerCase().includes(needle),
  );

  return (
    <>
      <NewPostCard onPosted={reload} identity={identity} />
      {failed ? (
        <Card title="Bulletin" subtitle="Pinned notes">
          <EmptyState icon="📌" message="Could not load posts" hint="Is the backend reachable?" />
        </Card>
      ) : posts === null ? (
        <Card title="Bulletin" subtitle="Pinned notes">
          <EmptyState icon="📌" message="Loading…" />
        </Card>
      ) : visible.length === 0 ? (
        <Card title="Bulletin" subtitle="Pinned notes">
          <EmptyState
            icon="📌"
            message={needle ? "No matching notes" : "No notes yet"}
            hint={
              needle
                ? posts.length < total
                  ? "No match in the loaded notes — try Load more or a different filter."
                  : "Try a different filter."
                : "Post the first note above."
            }
          />
        </Card>
      ) : (
        visible.map((post) => <PostCard key={post.id} post={post} onChanged={reload} identity={identity} />)
      )}
      {posts !== null && posts.length < total ? (
        <div style={{ marginTop: "var(--space-3)" }}>
          <button type="button" className="btn" disabled={loadingMore} onClick={loadMore}>
            {loadingMore ? "Loading…" : `Load more (${total - posts.length} older)`}
          </button>
        </div>
      ) : null}
    </>
  );
}
