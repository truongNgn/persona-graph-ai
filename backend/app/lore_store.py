"""Character lore retrieval.

`lore_vectorstore` is the single Chroma collection shared between the ingest
CLI (lore_ingest.py, writes child chunks) and retrieval (this module, reads
them) — both must point at the same collection/persist_directory or nothing
written by one is visible to the other.

`retrieve_character_context()` combines:
  - pinned sections (Identity / Personality / Speech Style) — always
    included regardless of the query, so short turns ("ừ", "kể tiếp đi")
    can't retrieve-miss the character's voice entirely.
  - similarity-retrieved parents for the actual query, deduped against the
    pinned set.
  - a hard character budget (`settings.lore_max_context_chars`) so lore
    can't silently crowd out memory/history in the prompt.

Fails soft everywhere: any error (missing parent store, Chroma unavailable,
embedding mismatch) returns "" rather than raising, so chat still works via
the persona anchor + settings fallback (see CLAUDE.md §3 / persona.py).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from langchain_chroma import Chroma

from app.config import settings
from app.memory_store import embeddings, embedding_signature

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Sections always injected regardless of the query — see module docstring.
# Matched against the top-level section name (before " / <subsection>").
_PINNED_SECTIONS = ("Identity", "Personality", "Speech Style")

# Separate collection from `chat_memories` (memory_store.py), same Chroma
# persist_directory — isolation between characters is by `character_id`
# metadata filter, not by a collection per character.
lore_vectorstore = Chroma(
    collection_name="character_lore_child",
    embedding_function=embeddings,
    persist_directory=settings.chroma_path,
)

# Cold start: loaded lazily on first retrieval call, not at import time, so
# a missing/not-yet-ingested parent store can't slow down or crash app
# startup — see _get_parent_store().
_parent_store_cache: dict[str, dict] | None = None
_mismatch_warned: set[str] = set()


def _parent_store_path() -> Path:
    lore_dir = Path(settings.lore_data_path)
    if not lore_dir.is_absolute():
        # settings.lore_data_path defaults to "./lore_data", relative to the
        # backend/ working directory the app normally runs from — must match
        # lore_ingest.py's _parent_store_path().
        lore_dir = PROJECT_ROOT / "backend" / lore_dir
    return lore_dir / "parent_store.json"


def _get_parent_store() -> dict[str, dict]:
    global _parent_store_cache
    if _parent_store_cache is not None:
        return _parent_store_cache

    path = _parent_store_path()
    if not path.exists():
        logger.warning(
            "Lore parent store not found at %s — character lore retrieval will "
            "return empty context until `python -m app.lore_ingest --all` is run.",
            path,
        )
        _parent_store_cache = {}
        return _parent_store_cache

    try:
        _parent_store_cache = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Failed to read lore parent store %s: %s", path, exc)
        _parent_store_cache = {}
    return _parent_store_cache


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _pinned_parent_ids(character_id: str, parent_store: dict[str, dict]) -> list[str]:
    by_top_section: dict[str, list[str]] = {}
    for parent_id, record in parent_store.items():
        if record.get("character_id") != character_id:
            continue
        top_section = str(record.get("section", "")).split(" / ")[0]
        by_top_section.setdefault(top_section, []).append(parent_id)

    pinned: list[str] = []
    for section in _PINNED_SECTIONS:
        pinned.extend(by_top_section.get(section, []))
    return pinned


def _warn_on_embedding_mismatch(character_id: str, parent_store: dict[str, dict]) -> None:
    """Detect the case where lore was ingested with a different embedding
    than the one currently configured (settings.embedding_provider changed
    after ingest) — vector search would silently return irrelevant results
    otherwise. Warns once per character per process, not per query."""
    if character_id in _mismatch_warned:
        return
    current_signature = embedding_signature()
    for record in parent_store.values():
        if record.get("character_id") != character_id:
            continue
        stamped = record.get("embedding_signature")
        if stamped and stamped != current_signature:
            logger.warning(
                "Character '%s' lore was ingested with embedding '%s' but the app "
                "is currently running '%s' — vector search results will be "
                "unreliable. Re-run `python -m app.lore_ingest --character %s`.",
                character_id,
                stamped,
                current_signature,
                character_id,
            )
        _mismatch_warned.add(character_id)
        return


async def retrieve_character_context(
    character_id: str,
    query: str,
    k: int = settings.lore_top_k,
) -> str:
    if not character_id:
        return ""

    try:
        parent_store = _get_parent_store()
        if not parent_store:
            return ""

        _warn_on_embedding_mismatch(character_id, parent_store)

        pinned_ids = _pinned_parent_ids(character_id, parent_store)

        child_docs = await lore_vectorstore.asimilarity_search(
            query=query, k=k, filter={"character_id": character_id}
        )
        retrieved_ids = [d.metadata["parent_id"] for d in child_docs if "parent_id" in d.metadata]

        ordered_ids = _dedup_preserve_order(pinned_ids + retrieved_ids)
        if not ordered_ids:
            return ""

        budget = settings.lore_max_context_chars
        used = 0
        blocks: list[str] = []
        for parent_id in ordered_ids:
            record = parent_store.get(parent_id)
            if not record:
                continue
            block = f"## {record.get('section', parent_id)}\n{record['text']}"
            # Always include at least the first block even if it alone
            # exceeds the budget (pinned sections are short by design, but
            # this avoids returning nothing on a pathological single block).
            if blocks and used + len(block) > budget:
                break
            blocks.append(block)
            used += len(block)

        if not blocks:
            return ""

        return "Character background:\n\n" + "\n\n".join(blocks)

    except Exception as exc:
        logger.error(
            "Character lore retrieval failed for character=%s query=%r: %s",
            character_id,
            query[:80],
            exc,
        )
        return ""
