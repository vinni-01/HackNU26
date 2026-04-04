from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException

router = APIRouter(prefix="/files", tags=["files"])

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    target = UPLOAD_DIR / file.filename
    contents = await file.read()
    target.write_bytes(contents)

    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "saved_to": str(target),
    }