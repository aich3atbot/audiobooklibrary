"""Minimal socket.io shim: the ABS app connects after login and emits
'auth' with its token; reply 'init' so it shows connected. All other
real-time events are optional pushes we never send."""

import logging

import socketio

from app.abs.tokens import verify_token
from app.config import get_settings

logger = logging.getLogger(__name__)

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")


@sio.event
async def connect(sid, environ):
    logger.debug("socket.io client connected: %s", sid)


@sio.event
async def auth(sid, token):
    settings = get_settings()
    if settings.auth_enabled:
        payload = verify_token(token) if isinstance(token, str) else None
        if payload is None or payload.get("type") == "refresh":
            await sio.emit("auth_failed", {"message": "Invalid token"}, to=sid)
            return
        username = payload["username"]
        user_id = payload["userId"]
    else:
        username = settings.auth_username or "user"
        user_id = "open"
    await sio.emit(
        "init", {"userId": user_id, "username": username, "usersOnline": []}, to=sid
    )


def wrap_asgi(fastapi_app):
    return socketio.ASGIApp(sio, other_asgi_app=fastapi_app)
