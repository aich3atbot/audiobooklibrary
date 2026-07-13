"""Minimal socket.io shim: the ABS app connects after login and emits
'auth' with its token; reply 'init' so it shows connected. All other
real-time events are optional pushes we never send."""

import logging

import socketio
from sqlalchemy import select

from app.abs.tokens import verify_token
from app.db import get_sessionmaker
from app.models import User

logger = logging.getLogger(__name__)

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")


@sio.event
async def connect(sid, environ):
    logger.debug("socket.io client connected: %s", sid)


@sio.event
async def auth(sid, token):
    payload = verify_token(token) if isinstance(token, str) else None
    if payload is None or payload.get("type") == "refresh":
        await sio.emit("auth_failed", {"message": "Invalid token"}, to=sid)
        return
    with get_sessionmaker()() as db:
        user = db.scalar(select(User).where(User.uuid == payload.get("userId", "")))
    if user is None or not user.enabled:
        await sio.emit("auth_failed", {"message": "Invalid token"}, to=sid)
        return
    await sio.emit(
        "init", {"userId": user.uuid, "username": user.username, "usersOnline": []}, to=sid
    )


def wrap_asgi(fastapi_app):
    return socketio.ASGIApp(sio, other_asgi_app=fastapi_app)
