
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    Path as PathParam,
    Request,
)
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.deps import get_current_admin
from app.models.user import User


router = APIRouter(
    tags=["Broadcast Web"],
)

BASE_DIR = Path(__file__).resolve().parents[1]

templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates"),
)


# ============================================
# ЦЕНТР ЭФИРОВ В АДМИН-ПАНЕЛИ
# ============================================

@router.get(
    "/admin/broadcast",
    response_class=HTMLResponse,
    name="admin_broadcast",
)
async def admin_broadcast(
    request: Request,
    admin: User = Depends(get_current_admin),
):
    return templates.TemplateResponse(
        request=request,
        name="admin_broadcast.html",
        context={
            "user": admin,
        },
    )


# ============================================
# ОТДЕЛЬНЫЙ ЦЕНТР ТРАНСЛЯЦИЙ
# ============================================

@router.get(
    "/broadcast/control",
    response_class=HTMLResponse,
    name="broadcast_control",
)
async def broadcast_control(
    request: Request,
    admin: User = Depends(get_current_admin),
):
    return templates.TemplateResponse(
        request=request,
        name="broadcast/control.html",
        context={
            "user": admin,
        },
    )


# ============================================
# ОПЕРАТОРСКАЯ ПАНЕЛЬ
# ============================================

@router.get(
    "/broadcast/studio/{session_id}",
    response_class=HTMLResponse,
    name="broadcast_studio",
)
async def broadcast_studio(
    request: Request,
    session_id: int = PathParam(ge=1),
    admin: User = Depends(get_current_admin),
):
    return templates.TemplateResponse(
        request=request,
        name="broadcast/studio.html",
        context={
            "session_id": session_id,
            "user": admin,
        },
    )


# ============================================
# ПУБЛИЧНЫЙ ОВЕРЛЕЙ OBS / vMix
# ============================================

@router.get(
    "/broadcast/overlay/{session_id}",
    response_class=HTMLResponse,
    name="broadcast_overlay",
)
async def broadcast_overlay(
    request: Request,
    session_id: int = PathParam(ge=1),
):
    return templates.TemplateResponse(
        request=request,
        name="broadcast/overlay.html",
        context={
            "session_id": session_id,
        },
    )


# ============================================
# LIVE-ТАБЛО ТУРНИРА
# ============================================

@router.get(
    "/broadcast/tournament/{tournament_id}/live",
    response_class=HTMLResponse,
    name="broadcast_tournament_live",
)
async def broadcast_tournament_live(
    request: Request,
    tournament_id: int = PathParam(ge=1),
):
    return templates.TemplateResponse(
        request=request,
        name="broadcast/tournament_live.html",
        context={
            "tournament_id": tournament_id,
        },
    )
