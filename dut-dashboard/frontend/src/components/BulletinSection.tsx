import { useCallback, useEffect, useState } from "react";

import {
  BulletinComment,
  BulletinPost,
  createBulletinComment,
  createBulletinPost,
  deleteBulletinPost,
  getBulletinPosts,
  humanizeApiError,
} from "../api/rest";
import { loadDisplayName } from "../monitoring/useSettings";
import { Card, EmptyState } from "./shell/Card";

function formatTime(iso: string): string {
  return iso.replace("T", " ");
}

function countReplies(comments: BulletinComment[]): number {
  return comments.reduce((total, c) => total + 1 + countReplies(c.replies), 0);
}

function NewPostCard({ onPosted }: { onPosted: () => void }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await createBulletinPost(title, body, loadDisplayName() || null);
      setTitle("");
      setBody("");
      onPosted();
    } catch (e) {
      setError(humanizeApiError(e));
    } finally {
      setBusy(false);
    }
  }, [title, body, onPosted]);

  const canPost = title.trim().length > 0 && body.trim().length > 0 && !busy;

  return (
    <Card title="New note" subtitle="Pin a note to the board">
      <div style={{ display: "grid", gap: "var(--space-3)" }}>
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

function ReplyBox({ postId, parentId, onReplied }: { postId: number; parentId?: number; onReplied: () => void }) {
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await createBulletinComment(postId, body, loadDisplayName() || null, parentId ?? null);
      setBody("");
      onReplied();
    } catch (e) {
      setError(humanizeApiError(e));
    } finally {
      setBusy(false);
    }
  }, [postId, parentId, body, onReplied]);

  return (
    <div style={{ display: "flex", gap: "var(--space-2)", marginTop: "var(--space-2)" }}>
      <input
        type="text"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && body.trim() && !busy && void submit()}
        placeholder={parentId ? "Reply…" : "Add a reply…"}
        aria-label="Reply"
        maxLength={600}
        style={{ flex: 1 }}
      />
      <button type="button" className="btn" disabled={!body.trim() || busy} onClick={() => void submit()}>
        {busy ? "…" : "Reply"}
      </button>
      {error ? <span className="flash" style={{ color: "var(--danger)" }}>{error}</span> : null}
    </div>
  );
}

function CommentThread({ comment, postId, onReplied }: { comment: BulletinComment; postId: number; onReplied: () => void }) {
  const [replying, setReplying] = useState(false);
  return (
    <div className="note" style={{ marginBottom: "var(--space-2)" }}>
      <p>{comment.body}</p>
      <div className="meta">
        {comment.author ?? "—"} · {formatTime(comment.created_at)} ·{" "}
        <button type="button" className="linklike" onClick={() => setReplying((v) => !v)}>
          Reply
        </button>
      </div>
      {replying ? (
        <ReplyBox
          postId={postId}
          parentId={comment.id}
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
                {reply.author ?? "—"} · {formatTime(reply.created_at)}
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function PostCard({ post, onChanged }: { post: BulletinPost; onChanged: () => void }) {
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
      subtitle={`${post.author ?? "—"} · ${formatTime(post.created_at)} · ${replies} ${replies === 1 ? "reply" : "replies"}`}
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
            <CommentThread key={comment.id} comment={comment} postId={post.id} onReplied={onChanged} />
          ))}
          <ReplyBox postId={post.id} onReplied={onChanged} />
        </div>
      ) : null}
    </Card>
  );
}

export default function BulletinSection({ query = "" }: { query?: string }) {
  const [posts, setPosts] = useState<BulletinPost[] | null>(null);
  const [failed, setFailed] = useState(false);

  const reload = useCallback(() => {
    setFailed(false);
    getBulletinPosts()
      .then(setPosts)
      .catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const needle = query.trim().toLowerCase();
  const visible = (posts ?? []).filter(
    (p) => !needle || p.title.toLowerCase().includes(needle) || p.body.toLowerCase().includes(needle),
  );

  return (
    <>
      <NewPostCard onPosted={reload} />
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
            hint={needle ? "Try a different filter." : "Post the first note above."}
          />
        </Card>
      ) : (
        visible.map((post) => <PostCard key={post.id} post={post} onChanged={reload} />)
      )}
    </>
  );
}
