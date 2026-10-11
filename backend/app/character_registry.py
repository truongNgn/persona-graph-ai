"""Character registry for multi-character roleplay metadata.

Loads character metadata from characters.json at the repo root (NOT under
docs/, which is gitignored — character data must be tracked and shipped in
deploys so users can pick a character in production). Falls back to a
single settings-derived character if the registry file is missing/empty.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "characters" / "characters.json"

# Fields that are internal wiring, not display data — never leak these into
# public_dict()/metadata, since character_registry.get()/list_public() feed
# directly into the unauthenticated GET /api/characters response.
_INTERNAL_FIELDS = {
    "id", "display_name", "name", "voice", "avatar", "avatar_thumbnail",
    "description", "brain_path",
}


def resolve_avatar_url(avatar_key: str | None) -> str | None:
    """Resolve a character's logical avatar key (e.g. "luna.glb") to a URL.

    `avatar` in characters.json is deliberately a key, not a filesystem path
    or environment-specific URL (see docs/RAG_character_roleplay_implementation_plan.md
    §1.3) — the deployment target decides how it's served:
      - GCS_BUCKET_NAME set (cloud deploy): public GCS object URL.
      - otherwise (local dev): static path served from frontend/public/models,
        matching the existing GET /api/models convention.
    """
    if not avatar_key:
        return None
    if settings.gcs_bucket_name:
        return f"https://storage.googleapis.com/{settings.gcs_bucket_name}/avatars/{avatar_key}"
    return f"/models/{avatar_key}"


@dataclass(frozen=True)
class Character:
    id: str
    display_name: str
    voice: str | None = None
    avatar: str | None = None
    avatar_thumbnail: str | None = None
    description: str = ""
    # Internal wiring — resolved server-side (lore_ingest.py / lore_store.py),
    # never serialized by public_dict(). Kept as an explicit field rather than
    # inside `metadata` so excluding it from the public API can't accidentally
    # make it unreachable internally too.
    brain_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "voice": self.voice,
            "avatar": resolve_avatar_url(self.avatar),
            "avatar_thumbnail": resolve_avatar_url(self.avatar_thumbnail),
            "description": self.description,
            **self.metadata,
        }


class CharacterRegistry:
    def __init__(self, registry_path: Path = DEFAULT_REGISTRY_PATH) -> None:
        self._registry_path = registry_path
        self._characters = self._load_characters()

    def get(self, character_id: str | None) -> dict[str, Any] | None:
        """Public-safe character dict — this is what reaches API responses."""
        if not character_id:
            return None
        character = self._characters.get(character_id)
        return character.public_dict() if character else None

    def list_public(self) -> list[dict[str, Any]]:
        return [character.public_dict() for character in self._characters.values()]

    def brain_path(self, character_id: str) -> str | None:
        """Server-internal only — used by lore_ingest.py/lore_store.py to
        locate a character's lore document. Never expose this through an API
        response; use get()/list_public() for anything client-facing."""
        character = self._characters.get(character_id)
        return character.brain_path if character else None

    def ids(self) -> list[str]:
        """All registered character ids — used by `lore_ingest.py --all`."""
        return list(self._characters.keys())

    def _load_characters(self) -> dict[str, Character]:
        loaded = self._load_from_json()
        if loaded:
            return loaded

        fallback = Character(
            id=settings.default_character_id,
            display_name=settings.character_name or settings.default_character_id.title(),
            voice=None,
            avatar=None,
            description=settings.character_persona,
        )
        logger.warning(
            "Character registry file not found or empty at %s; using fallback character '%s'.",
            self._registry_path,
            fallback.id,
        )
        return {fallback.id: fallback}

    def _load_from_json(self) -> dict[str, Character]:
        if not self._registry_path.exists():
            return {}

        try:
            raw = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read character registry %s: %s", self._registry_path, exc)
            return {}

        items = raw.get("characters", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            logger.warning("Character registry %s must contain a list.", self._registry_path)
            return {}

        characters: dict[str, Character] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            character_id = str(item.get("id", "")).strip()
            if not character_id:
                continue
            metadata = {
                key: value
                for key, value in item.items()
                if key not in _INTERNAL_FIELDS
            }
            characters[character_id] = Character(
                id=character_id,
                display_name=str(item.get("display_name") or item.get("name") or character_id),
                voice=item.get("voice"),
                avatar=item.get("avatar"),
                avatar_thumbnail=item.get("avatar_thumbnail"),
                description=str(item.get("description", "")),
                brain_path=item.get("brain_path"),
                metadata=metadata,
            )

        return characters


character_registry = CharacterRegistry()
