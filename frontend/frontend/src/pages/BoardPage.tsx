import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Tldraw, getSnapshot, loadSnapshot, Editor } from "tldraw";
import "tldraw/tldraw.css";
import { getBoard, updateBoard } from "../api/boards";
import { API_BASE_URL } from "../api/client";

type Board = {
  id: string;
  title: string;
  description?: string;
  content?: any;
};

type SyncState = "connecting" | "connected" | "reconnecting" | "disconnected";

function getBoardWsUrl(boardId: string) {
  const apiUrl = new URL(API_BASE_URL);
  const wsProtocol = apiUrl.protocol === "https:" ? "wss:" : "ws:";
  return `${wsProtocol}//${apiUrl.host}/ws/boards/${boardId}`;
}

export default function BoardPage() {
  const { id } = useParams();

  const [board, setBoard] = useState<Board | null>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [syncState, setSyncState] = useState<SyncState>("disconnected");

  const editorRef = useRef<Editor | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const saveTimeoutRef = useRef<number | null>(null);
  const syncTimeoutRef = useRef<number | null>(null);
  const isHydratingRef = useRef(false);

  useEffect(() => {
    async function loadBoardData() {
      if (!id) {
        setLoading(false);
        setError("Board id is missing");
        return;
      }

      try {
        setError("");
        setLoading(true);

        const data = await getBoard(id);

        setBoard(data);
        setTitle(data.title);
        setDescription(data.description || "");
      } catch (err: any) {
        setError(err?.response?.data?.detail || "Failed to load board");
      } finally {
        setLoading(false);
      }
    }

    loadBoardData();

    return () => {
      if (saveTimeoutRef.current) {
        window.clearTimeout(saveTimeoutRef.current);
      }
      if (syncTimeoutRef.current) {
        window.clearTimeout(syncTimeoutRef.current);
      }
      socketRef.current?.close();
    };
  }, [id]);

  useEffect(() => {
    if (!id) return;

    let socket: WebSocket | null = null;
    let reconnectTimeout: number | null = null;
    let shouldReconnect = true;
    let reconnectAttempts = 0;

    const connect = () => {
      setSyncState(reconnectAttempts === 0 ? "connecting" : "reconnecting");
      socket = new WebSocket(getBoardWsUrl(id));
      socketRef.current = socket;

      socket.onopen = () => {
        reconnectAttempts = 0;
        setSyncState("connected");
      };

      socket.onmessage = (event) => {
        if (!editorRef.current) return;

        try {
          const snapshot = JSON.parse(event.data);
          isHydratingRef.current = true;
          loadSnapshot(editorRef.current.store, snapshot);
        } catch (err) {
          console.error("Failed to apply remote canvas update", err);
        } finally {
          window.setTimeout(() => {
            isHydratingRef.current = false;
          }, 0);
        }
      };

      socket.onerror = () => {
        socket?.close();
      };

      socket.onclose = () => {
        if (socketRef.current === socket) {
          socketRef.current = null;
        }

        if (!shouldReconnect) {
          setSyncState("disconnected");
          return;
        }

        reconnectAttempts += 1;
        const delay = Math.min(1000 * 2 ** Math.min(reconnectAttempts, 4), 10000);
        setSyncState("reconnecting");
        reconnectTimeout = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      shouldReconnect = false;
      if (reconnectTimeout) {
        window.clearTimeout(reconnectTimeout);
      }
      socket?.close();
    };
  }, [id]);

  async function saveBoard(customTitle?: string, customDescription?: string) {
    if (!id || !editorRef.current || !board) return;

    try {
      setSaving(true);
      setSaveError("");

      const snapshot = getSnapshot(editorRef.current.store);

      const payload = {
        title: customTitle ?? title,
        description: customDescription ?? description,
        content: snapshot,
      };

      const updated = await updateBoard(id, payload);
      setBoard(updated);
    } catch (err: any) {
      console.error("Save failed:", err?.response?.data || err);
      setSaveError(err?.response?.data?.detail || "Failed to save board");
    } finally {
      setSaving(false);
    }
  }

  function scheduleSave() {
    if (!id || !editorRef.current || !board) return;
    if (isHydratingRef.current) return;

    if (saveTimeoutRef.current) {
      window.clearTimeout(saveTimeoutRef.current);
    }

    saveTimeoutRef.current = window.setTimeout(() => {
      void saveBoard();
    }, 1000);
  }

  function scheduleSync() {
    if (!editorRef.current) return;
    if (isHydratingRef.current) return;
    if (!socketRef.current || socketRef.current.readyState !== WebSocket.OPEN) return;

    if (syncTimeoutRef.current) {
      window.clearTimeout(syncTimeoutRef.current);
    }

    syncTimeoutRef.current = window.setTimeout(() => {
      if (!editorRef.current) return;
      const snapshot = getSnapshot(editorRef.current.store);
      socketRef.current?.send(JSON.stringify(snapshot));
    }, 150);
  }

  function handleMount(editor: Editor) {
    editorRef.current = editor;

    if (board?.content) {
      try {
        isHydratingRef.current = true;
        loadSnapshot(editor.store, board.content);
      } catch (err) {
        console.error("Failed to load snapshot", err);
      } finally {
        window.setTimeout(() => {
          isHydratingRef.current = false;
        }, 0);
      }
    }

    editor.store.listen(
      () => {
        scheduleSave();
        scheduleSync();
      },
      { scope: "document", source: "user" }
    );
  }

  async function handleSaveDetails() {
    await saveBoard(title, description);
  }

  const syncColor =
    syncState === "connected"
      ? "green"
      : syncState === "reconnecting" || syncState === "connecting"
      ? "#996b00"
      : "#555";

  return (
    <div style={{ maxWidth: 1200, margin: "20px auto", padding: "0 16px" }}>
      <p>
        <Link to="/boards">← Back to boards</Link>
      </p>

      {loading ? (
        <p>Loading board...</p>
      ) : error ? (
        <p style={{ color: "red" }}>{error}</p>
      ) : board ? (
        <>
          <div style={{ marginBottom: 16 }}>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Board title"
              style={{
                width: "100%",
                fontSize: 28,
                fontWeight: 700,
                padding: 8,
                marginBottom: 8,
              }}
            />

            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Board description"
              rows={2}
              style={{
                width: "100%",
                padding: 8,
                resize: "vertical",
                marginBottom: 8,
              }}
            />

            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <button onClick={handleSaveDetails}>Save details</button>

              <div style={{ textAlign: "right" }}>
                <p style={{ margin: 0 }}>{saving ? "Saving..." : "Saved"}</p>
                <p style={{ margin: "4px 0 0", color: syncColor }}>
                  Sync: {syncState}
                </p>
                {saveError ? (
                  <p style={{ margin: "4px 0 0", color: "red" }}>
                    {saveError}
                  </p>
                ) : null}
              </div>
            </div>
          </div>

          <div
            style={{
              height: "72vh",
              border: "1px solid #ccc",
              borderRadius: 12,
              overflow: "hidden",
            }}
          >
            <Tldraw onMount={handleMount} />
          </div>
        </>
      ) : (
        <p>Board not found</p>
      )}
    </div>
  );
}
