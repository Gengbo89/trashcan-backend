from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings
from src.routes.ai import router as ai_router
from src.routes.analytics import router as analytics_router
from src.routes.auth import router as auth_router
from src.routes.health import router as health_router
from src.routes.messages import router as messages_router
from src.routes.tools import router as tools_router
from src.services.auth import init_db
from src.services.analytics import init_analytics_db
from src.services.messages import init_message_db


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name)
    init_db()
    init_message_db()
    init_analytics_db()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(auth_router, prefix="/auth", tags=["auth"])
    app.include_router(messages_router, prefix="/messages", tags=["messages"])
    app.include_router(tools_router, prefix="/tools", tags=["tools"])
    app.include_router(analytics_router, prefix="/analytics", tags=["analytics"])
    app.include_router(ai_router, prefix="/ai", tags=["ai"])
    return app


app = create_app()
