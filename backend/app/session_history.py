"""Persistent chat history backend for LangChain session memory."""

from __future__ import annotations

import json
import threading
import time
from hashlib import sha256
from pathlib import Path

from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import BaseMessage, messages_from_dict, message_to_dict
from langchain_community.chat_message_histories import ChatMessageHistory

from app.config import BACKEND_ROOT, settings


_memory_store: dict[str, ChatMessageHistory] = {}
_file_locks: dict[Path, threading.Lock] = {}
_global_lock = threading.Lock()


def build_history_key(user_id: str, character_id: str, session_id: str) -> str:
    """Turn history is isolated per (user_id, character_id, session_id) —
    not just (user_id, session_id) — so that reusing a session_id across a
    character switch can't feed one character's conversation history into
    another character's prompt. Chat memory (memory_store.py) already
    isolates by (user_id, character_id); this closes the equivalent gap for
    LangChain's own turn history, which is keyed independently.
    """
    return f"{user_id}:{character_id}:{session_id}"


def _safe_filename(key: str) -> str:
    return sha256(key.encode("utf-8")).hexdigest() + ".jsonl"


def _history_root() -> Path:
    raw = Path(settings.session_history_path)
    if raw.is_absolute():
        return raw
    return BACKEND_ROOT / raw


def _lock_for(path: Path) -> threading.Lock:
    with _global_lock:
        if path not in _file_locks:
            _file_locks[path] = threading.Lock()
        return _file_locks[path]


class FileChatMessageHistory(BaseChatMessageHistory):
    def __init__(self, session_key: str) -> None:
        self.session_key = session_key
        self.path = _history_root() / _safe_filename(session_key)

    @property
    def messages(self) -> list[BaseMessage]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        with _lock_for(self.path):
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        messages = [row["message"] for row in rows if row.get("message")]
        max_messages = max(settings.max_history_turns * 2, 2)
        return messages_from_dict(messages[-max_messages:])

    def add_message(self, message: BaseMessage) -> None:
        self.add_messages([message])

    def add_messages(self, messages: list[BaseMessage]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _lock_for(self.path):
            with self.path.open("a", encoding="utf-8") as f:
                for message in messages:
                    row = {
                        "session_key": self.session_key,
                        "created_at": time.time(),
                        "message": message_to_dict(message),
                    }
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def clear(self) -> None:
        with _lock_for(self.path):
            if self.path.exists():
                self.path.unlink()


def get_session_history(session_key: str) -> BaseChatMessageHistory:
    if settings.session_backend.lower() == "memory":
        if session_key not in _memory_store:
            _memory_store[session_key] = ChatMessageHistory()
        return _memory_store[session_key]
    return FileChatMessageHistory(session_key)


def session_history_ready() -> bool:
    try:
        root = _history_root()
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".ready"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False
