from collections import defaultdict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.db.database import Base, engine
from app.models.board import Board
from app.models.user import User
from app.routers.auth import router as auth_router
from app.routers.boards import router as boards_router

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Hackathon Backend")

app.include_router(auth_router)
app.include_router(boards_router)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class BoardSyncManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, board_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[board_id].add(websocket)

    def disconnect(self, board_id: str, websocket: WebSocket) -> None:
        if board_id not in self._connections:
            return

        self._connections[board_id].discard(websocket)

        if not self._connections[board_id]:
            del self._connections[board_id]

    async def broadcast(self, board_id: str, sender: WebSocket, message: str) -> None:
        sockets = self._connections.get(board_id)
        if not sockets:
            return

        stale: list[WebSocket] = []
        for socket in sockets:
            if socket == sender:
                continue
            try:
                await socket.send_text(message)
            except Exception:
                stale.append(socket)

        for socket in stale:
            self.disconnect(board_id, socket)


sync_manager = BoardSyncManager()


@app.websocket("/ws/boards/{board_id}")
async def board_sync_socket(websocket: WebSocket, board_id: str):
    await sync_manager.connect(board_id, websocket)

    try:
        while True:
            message = await websocket.receive_text()
            await sync_manager.broadcast(board_id, websocket, message)
    except WebSocketDisconnect:
        sync_manager.disconnect(board_id, websocket)


@app.get("/")
def root():
    return {"message": "API is running"}
