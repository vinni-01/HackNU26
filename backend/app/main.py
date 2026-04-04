from fastapi import FastAPI

from app.db.database import Base, engine
from app.models.user import User
from app.models.board import Board
from app.routers.auth import router as auth_router
from app.routers.boards import router as boards_router
from fastapi.middleware.cors import CORSMiddleware

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

@app.get("/")
def root():
    return {"message": "API is running"}
