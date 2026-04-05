import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  createBoard,
  deleteBoard,
  getBoards,
  getDiscoverBoards,
  type Board,
} from "../api/boards";
import { useAuth } from "../context/AuthContext";

export default function BoardsPage() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const [boards, setBoards] = useState<Board[]>([]);
  const [discoverBoards, setDiscoverBoards] = useState<Board[]>([]);
  const [title, setTitle] = useState("");
  const [joinBoardId, setJoinBoardId] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [error, setError] = useState("");

  async function loadBoards() {
    try {
      setError("");
      setLoading(true);
      const [mine, discover] = await Promise.all([getBoards(), getDiscoverBoards()]);
      setBoards(mine);
      setDiscoverBoards(discover.filter((board) => String(board.owner_id) !== String(user?.id)));
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to load boards");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadBoards();
  }, [user?.id]);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;

    try {
      setCreating(true);
      setError("");

      const newBoard = await createBoard({
        title: title.trim(),
        description: "No description",
      });

      setBoards((prev) => [newBoard, ...prev]);
      setTitle("");
      void loadBoards();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to create board");
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(id: string) {
    if (!window.confirm("Delete this board?")) return;

    try {
      setDeletingId(id);
      setError("");
      await deleteBoard(id);
      setBoards((prev) => prev.filter((board) => board.id !== id));
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to delete board");
    } finally {
      setDeletingId(null);
    }
  }

  function handleConnectBoard(e: FormEvent) {
    e.preventDefault();
    const targetBoardId = joinBoardId.trim();
    if (!targetBoardId) {
      setError("Please provide a board id to connect");
      return;
    }

    setError("");
    navigate(`/boards/${targetBoardId}`);
  }

  async function copyBoardId(boardId: string) {
    try {
      await navigator.clipboard.writeText(boardId);
    } catch {
      setError("Could not copy board id. Please copy it manually.");
    }
  }

  return (
    <div className="page-shell">
      <div className="toolbar">
        <div>
          <h1 className="page-title" style={{ marginBottom: 4 }}>
            Boards
          </h1>
          <p className="muted">{user?.email}</p>
        </div>
        <button className="ghost-btn" onClick={logout}>
          Logout
        </button>
      </div>

      <div className="glass-card" style={{ padding: 16, marginBottom: 16 }}>
        <form onSubmit={handleCreate} className="form-row">
          <input
            className="input"
            type="text"
            placeholder="Board title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <button type="submit" disabled={creating}>
            {creating ? "Creating..." : "Create board"}
          </button>
        </form>

        <form onSubmit={handleConnectBoard} className="form-row" style={{ marginBottom: 0 }}>
          <input
            className="input"
            type="text"
            placeholder="Paste board ID to connect"
            value={joinBoardId}
            onChange={(e) => setJoinBoardId(e.target.value)}
          />
          <button type="submit">Connect</button>
        </form>
      </div>

      {error && <p className="error-text">{error}</p>}

      {loading ? (
        <p className="muted">Loading boards...</p>
      ) : (
        <>
          <h2 style={{ marginBottom: 10 }}>Your boards</h2>
          {boards.length === 0 ? (
            <div className="glass-card" style={{ padding: 24, marginBottom: 20 }}>
              <p className="muted">No boards yet. Create your first one.</p>
            </div>
          ) : (
            <div className="board-grid" style={{ marginBottom: 24 }}>
              {boards.map((board) => (
                <div key={board.id} className="glass-card board-item">
                  <div>
                    <Link to={`/boards/${board.id}`} style={{ fontSize: 18, fontWeight: 700 }}>
                      {board.title}
                    </Link>
                    <p className="muted" style={{ marginTop: 6 }}>
                      {board.description || "No description"}
                    </p>
                    <p className="muted" style={{ marginTop: 6, fontSize: 13 }}>
                      Board ID: <code>{board.id}</code>
                    </p>
                  </div>

                  <div style={{ display: "flex", gap: 8 }}>
                    <button className="ghost-btn" onClick={() => void copyBoardId(board.id)}>
                      Copy ID
                    </button>
                    <button
                      className="ghost-btn"
                      onClick={() => handleDelete(board.id)}
                      disabled={deletingId === board.id}
                    >
                      {deletingId === board.id ? "Deleting..." : "Delete"}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          <h2 style={{ marginBottom: 10 }}>Discover shared boards</h2>
          {discoverBoards.length === 0 ? (
            <div className="glass-card" style={{ padding: 24 }}>
              <p className="muted">No boards from other users yet.</p>
            </div>
          ) : (
            <div className="board-grid">
              {discoverBoards.map((board) => (
                <div key={board.id} className="glass-card board-item">
                  <div>
                    <Link to={`/boards/${board.id}`} style={{ fontSize: 18, fontWeight: 700 }}>
                      {board.title}
                    </Link>
                    <p className="muted" style={{ marginTop: 6 }}>
                      {board.description || "No description"}
                    </p>
                    <p className="muted" style={{ marginTop: 6, fontSize: 13 }}>
                      Board ID: <code>{board.id}</code>
                    </p>
                  </div>

                  <div style={{ display: "flex", gap: 8 }}>
                    <button onClick={() => navigate(`/boards/${board.id}`)}>Open</button>
                    <button className="ghost-btn" onClick={() => void copyBoardId(board.id)}>
                      Copy ID
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
