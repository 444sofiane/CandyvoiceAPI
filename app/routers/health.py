from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health():
    # Deliberately minimal for a public endpoint — no executable paths,
    # project IDs, or auth-mode internals.
    return {"ok": True}
