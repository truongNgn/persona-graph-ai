"""Postgres-backed users, conversations, and messages repository."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert

from app.db import async_session_factory
from app.db_models import Conversation, Message, User

MessageRole = Literal["human", "ai"]


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class ConversationSummary(BaseModel):
    id: str
    title: str | None = None
    character_id: str
    created_at: float
    updated_at: float


class MessageRecord(BaseModel):
    id: int
    role: MessageRole
    content: str
    emotion: str | None = None
    turn_id: str | None = None
    created_at: float


class ConversationDetail(BaseModel):
    conversation: ConversationSummary
    messages: list[MessageRecord]


@dataclass(frozen=True)
class StoredUser:
    id: str
    email: str
    display_name: str | None


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"pbkdf2_sha256$120000${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt_hex, digest_hex = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations_raw),
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _user_dict(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "created_at": user.created_at,
    }


def _conversation_summary(conversation: Conversation) -> ConversationSummary:
    return ConversationSummary(
        id=conversation.id,
        title=conversation.title,
        character_id=conversation.character_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _message_record(message: Message) -> MessageRecord:
    return MessageRecord(
        id=message.id,
        role=message.role,  # type: ignore[arg-type]
        content=message.content,
        emotion=message.emotion,
        turn_id=message.turn_id,
        created_at=message.created_at,
    )


async def create_user(payload: RegisterRequest) -> User:
    async with async_session_factory() as session:
        existing = await session.scalar(select(User).where(User.email == payload.email.lower()))
        if existing:
            raise ValueError("Email is already registered.")
        user = User(
            email=payload.email.lower(),
            password_hash=hash_password(payload.password),
            display_name=payload.display_name,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def authenticate_user(payload: LoginRequest) -> User | None:
    async with async_session_factory() as session:
        user = await session.scalar(select(User).where(User.email == payload.email.lower()))
        if not user or not verify_password(payload.password, user.password_hash):
            return None
        return user


async def get_user(user_id: str) -> User | None:
    async with async_session_factory() as session:
        return await session.get(User, user_id)


DEV_QUICK_LOGIN_USER_ID = "00000000-0000-4000-8000-000000000001"
DEV_QUICK_LOGIN_EMAIL = "dev-quick-login@localhost.invalid"


async def get_or_create_dev_user() -> User:
    """Reuse a dedicated test identity; concurrent clicks create only one row."""
    async with async_session_factory() as session:
        await session.execute(
            insert(User).values(
                id=DEV_QUICK_LOGIN_USER_ID,
                email=DEV_QUICK_LOGIN_EMAIL,
                display_name="Dev Tester",
                password_hash="dev_quick_login_no_password",
            ).on_conflict_do_nothing(index_elements=[User.id])
        )
        await session.commit()
        user = await session.get(User, DEV_QUICK_LOGIN_USER_ID)
        if user is None or user.email != DEV_QUICK_LOGIN_EMAIL or user.password_hash != "dev_quick_login_no_password":
            raise ValueError("The reserved development identity is unavailable.")
        return user


async def get_or_create_google_user(email: str, display_name: str | None) -> User:
    async with async_session_factory() as session:
        user = await session.scalar(select(User).where(User.email == email.lower()))
        if not user:
            user = User(
                email=email.lower(),
                password_hash="oauth_google_no_password",
                display_name=display_name,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


async def ensure_conversation(
    session: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
    character_id: str,
    title_seed: str | None = None,
) -> Conversation:
    conversation = await session.get(Conversation, conversation_id)
    if conversation:
        if conversation.user_id != user_id:
            raise PermissionError("Conversation belongs to another user.")
        changed = False
        if conversation.character_id != character_id:
            conversation.character_id = character_id
            changed = True
        if not conversation.title and title_seed:
            conversation.title = _title_from_text(title_seed)
            changed = True
        if changed:
            await session.flush()
        return conversation

    conversation = Conversation(
        id=conversation_id,
        user_id=user_id,
        character_id=character_id,
        title=_title_from_text(title_seed or "New Chat"),
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def append_message(
    *,
    user_id: str,
    conversation_id: str,
    character_id: str,
    role: MessageRole,
    content: str,
    turn_id: str | None = None,
    emotion: str | None = None,
) -> None:
    async with async_session_factory() as session:
        async with session.begin():
            conversation = await ensure_conversation(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                character_id=character_id,
                title_seed=content if role == "human" else None,
            )
            now_time = time.time()
            message = Message(
                conversation_id=conversation.id,
                role=role,
                content=content,
                turn_id=turn_id,
                emotion=emotion,
                created_at=now_time,
            )
            session.add(message)
            conversation.updated_at = now_time


async def list_conversations(user_id: str, limit: int = 50) -> list[ConversationSummary]:
    async with async_session_factory() as session:
        result = await session.scalars(
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
        return [_conversation_summary(item) for item in result]


async def get_conversation_detail(user_id: str, conversation_id: str) -> ConversationDetail | None:
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if not conversation or conversation.user_id != user_id:
            return None
        result = await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
        return ConversationDetail(
            conversation=_conversation_summary(conversation),
            messages=[_message_record(item) for item in result],
        )


async def delete_conversation(user_id: str, conversation_id: str) -> bool:
    async with async_session_factory() as session:
        async with session.begin():
            conversation = await session.get(Conversation, conversation_id)
            if not conversation or conversation.user_id != user_id:
                return False
            await session.delete(conversation)
            return True


def user_public_dict(user: User) -> dict:
    return _user_dict(user)


def _title_from_text(text: str) -> str:
    words = text.strip().split()
    title = " ".join(words[:8]) if words else "New Chat"
    return title[:200]
