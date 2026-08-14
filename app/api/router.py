"""Aggregate all versioned API routers."""

from fastapi import APIRouter

from app.api.v1 import auth, plants, submissions, users

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(plants.router, prefix="/plants", tags=["plants"])
api_router.include_router(submissions.router, prefix="/submissions", tags=["submissions"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
