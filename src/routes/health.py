from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health_check():
    return {"code": 200, "success": True, "data": {"status": "ok"}}
