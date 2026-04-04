import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { createBoard, deleteBoard, getBoards } from "../api/boards";
import { useAuth } from "../context/AuthContext";

type Board = {
  id: string;
  title: string;
  description?: string;
};

export default function BoardsPage() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const [boards, setBoards] = useState<Board[]>([]);
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
      const data = await getBoards();
      setBoards(data);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to load boards");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadBoards();
  }, []);

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

      <form onSubmit={handleCreate} style={{ marginBottom: 12 }}>
        <input
          type="text"
          placeholder="Board title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          style={{
            padding: 10,
            width: 320,
            marginRight: 8,
            borderRadius: 8,
            border: "1px solid #ccc",
          }}
        />
        <button type="submit" disabled={creating}>
          {creating ? "Creating..." : "Create board"}
        </button>
      </form>

      <form onSubmit={handleConnectBoard} style={{ marginBottom: 24 }}>
        <input
          type="text"
          placeholder="Other user's board id"
          value={joinBoardId}
          onChange={(e) => setJoinBoardId(e.target.value)}
          style={{
            padding: 10,
            width: 320,
            marginRight: 8,
            borderRadius: 8,
            border: "1px solid #ccc",
          }}
        />
        <button type="submit">Connect to board</button>
      </form>

      {error && <p style={{ color: "crimson" }}>{error}</p>}

      {loading ? (
        <p className="muted">Loading boards...</p>
      ) : boards.length === 0 ? (
        <div className="glass-card" style={{ padding: 24 }}>
          <p className="muted">No boards yet. Create your first one.</p>
        </div>
      ) : (
        <div className="board-grid">
          {boards.map((board) => (
            <div key={board.id} className="glass-card board-item">
              <div>
                <Link to={`/boards/${board.id}`} style={{ fontSize: 18, fontWeight: 700 }}>
                  {board.title}
                </Link>
                <p className="muted" style={{ marginTop: 6 }}>
                  {board.description || "No description"}
                </p>
              </div>

              <button
                className="ghost-btn"
                onClick={() => handleDelete(board.id)}
                disabled={deletingId === board.id}
              >
                {deletingId === board.id ? "Deleting..." : "Delete"}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
