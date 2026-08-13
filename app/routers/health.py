from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health():
    # Volontairement minimal pour un endpoint public — pas de chemins
    # d'exécutables, d'ID de projet, ni de détails internes du mode d'auth.
    return {"ok": True}
