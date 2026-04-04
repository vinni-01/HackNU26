import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { createBoard, deleteBoard, getBoards } from "../api/boards";
import { useAuth } from "../context/AuthContext";

type Board = {
  id: string;
  title: string;
  description?: string;
};

export default function BoardsPage() {
  const { user, logout } = useAuth();

  const [boards, setBoards] = useState<Board[]>([]);
  const [title, setTitle] = useState("");
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
      console.error("CREATE ERROR:", err?.response?.data || err);
      setError(err?.response?.data?.detail || "Failed to create board");
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(id: string) {
    const confirmed = window.confirm("Delete this board?");
    if (!confirmed) return;

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

  return (
    <div style={{ maxWidth: 900, margin: "40px auto", padding: "0 16px" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 24,
        }}
      >
        <div>
          <h1 style={{ marginBottom: 4 }}>Boards</h1>
          <p style={{ margin: 0, color: "#666" }}>{user?.email}</p>
        </div>

        <button onClick={logout}>Logout</button>
      </div>

      <form onSubmit={handleCreate} style={{ marginBottom: 24 }}>
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

      {error && <p style={{ color: "crimson" }}>{error}</p>}

      {loading ? (
        <p>Loading boards...</p>
      ) : boards.length === 0 ? (
        <div
          style={{
            padding: 24,
            border: "1px dashed #ccc",
            borderRadius: 12,
          }}
        >
          <p style={{ margin: 0 }}>No boards yet. Create your first one.</p>
        </div>
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          {boards.map((board) => (
            <div
              key={board.id}
              style={{
                border: "1px solid #ddd",
                borderRadius: 12,
                padding: 16,
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
              }}
            >
              <div>
                <Link
                  to={`/boards/${board.id}`}
                  style={{
                    fontSize: 18,
                    fontWeight: 600,
                    textDecoration: "none",
                  }}
                >
                  {board.title}
                </Link>

                <p style={{ margin: "6px 0 0", color: "#666" }}>
                  {board.description || "No description"}
                </p>
              </div>

              <button
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