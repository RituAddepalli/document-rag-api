import uuid

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
