"""Ingest character brain (`.md`) documents into the character lore store.

Two-tier structure-based chunking (design in
docs/CHARACTER_BRAIN_IMPLEMENTATION_PLAN.md §3):

  Tier 1 — MarkdownHeaderTextSplitter on `#`/`##` produces one parent
           section per (section, subsection) heading pair. Each parent is
           kept as full text in the JSON parent store.
  Tier 2 — a parent whose word count exceeds `lore_chunk_threshold_words` is
           further split into smaller child chunks for embedding; short
           parents are embedded as a single child equal to the whole parent.

Idempotent by design: re-ingesting a character first deletes its existing
Chroma child chunks and parent-store entries, then writes fresh ones with
deterministic ids — editing a character's lore and re-running never leaves
orphaned chunks from a previous version behind.

CLI:
    python -m app.lore_ingest --character luna
    python -m app.lore_ingest --all
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from app.character_registry import character_registry
from app.config import settings
from app.lore_store import lore_vectorstore
from app.memory_store import embedding_signature

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_HEADER_SPLITTER = MarkdownHeaderTextSplitter(headers_to_split_on=[("#", "section"), ("##", "subsection")])


def _slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text or "section"


def _section_slug(metadata: dict[str, str]) -> str:
    section = metadata.get("section", "")
    subsection = metadata.get("subsection")
    base = f"{section}-{subsection}" if subsection else section
    return _slugify(base)


def _section_label(metadata: dict[str, str]) -> str:
    section = metadata.get("section")
    subsection = metadata.get("subsection")
    if section and subsection:
        return f"{section} / {subsection}"
    return section or "root"


def _parent_store_path() -> Path:
    lore_dir = Path(settings.lore_data_path)
    if not lore_dir.is_absolute():
        # settings.lore_data_path defaults to "./lore_data", relative to the
        # backend/ working directory the app normally runs from.
        lore_dir = PROJECT_ROOT / "backend" / lore_dir
    lore_dir.mkdir(parents=True, exist_ok=True)
    return lore_dir / "parent_store.json"


def _load_parent_store() -> dict[str, dict[str, Any]]:
    path = _parent_store_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read parent store %s (starting fresh): %s", path, exc)
        return {}


def _save_parent_store(store: dict[str, dict[str, Any]]) -> None:
    path = _parent_store_path()
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def _split_sections(text: str, character_id: str) -> list[Document]:
    sections = _HEADER_SPLITTER.split_text(text)
    if sections:
        return sections
    # No headings at all -> whole file is one section (edge case in plan §5.2).
    logger.warning(
        "Character '%s' brain document has no Markdown headings; ingesting as a single section.",
        character_id,
    )
    return [Document(page_content=text.strip(), metadata={})]


def ingest_character(character_id: str) -> None:
    """Ingest (or re-ingest) a single character's brain document.

    Deletes any existing child chunks / parent entries for this
    character_id first, so this is safe to run repeatedly after editing
    the source `.md` — no orphaned chunks from a previous version survive.
    """
    brain_path = character_registry.brain_path(character_id)
    if not brain_path:
        raise ValueError(
            f"Character '{character_id}' has no brain_path configured in characters.json."
        )

    md_path = PROJECT_ROOT / brain_path
    if not md_path.exists():
        raise FileNotFoundError(f"Character brain document not found: {md_path}")

    text = md_path.read_text(encoding="utf-8")
    sections = _split_sections(text, character_id)
    signature = embedding_signature()

    # Idempotent re-ingest: wipe old child chunks for this character before
    # writing new ones. Without this, editing lore and re-running leaves
    # orphaned chunks from the previous version in Chroma (see module docstring).
    # Residual risk: delete succeeds but the add_documents() below fails
    # (e.g. embedding model unavailable) -> Chroma ends up empty for this
    # character until the run is repeated. Chroma's Python client has no
    # upsert in this version, so delete+add (not true upsert) is the
    # available primitive; accepted here since this is an offline CLI a
    # developer re-runs by hand, and lore_store.py fails soft on empty
    # retrieval rather than breaking chat (docs/CLAUDE.md §3).
    try:
        lore_vectorstore.delete(where={"character_id": character_id})
    except Exception as exc:
        logger.warning(
            "Could not clear existing lore chunks for '%s' (may not exist yet): %s",
            character_id,
            exc,
        )

    parent_store = _load_parent_store()
    parent_store = {
        parent_id: record
        for parent_id, record in parent_store.items()
        if record.get("character_id") != character_id
    }

    child_splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.lore_chunk_size_chars,
        chunk_overlap=settings.lore_chunk_overlap_chars,
        separators=["\n## ", "\n\n", "\n", ". ", " "],
    )

    child_docs: list[Document] = []
    child_ids: list[str] = []
    seen_slugs: dict[str, int] = {}
    parent_count = 0

    for section_doc in sections:
        content = section_doc.page_content.strip()
        if not content:
            continue

        slug = _section_slug(section_doc.metadata)
        idx = seen_slugs.get(slug, 0)
        seen_slugs[slug] = idx + 1
        parent_id = f"{character_id}_{slug}_{idx}"

        label = _section_label(section_doc.metadata)
        word_count = len(content.split())
        logger.info("[%s] section=%r word_count=%d parent_id=%s", character_id, label, word_count, parent_id)

        parent_store[parent_id] = {
            "parent_id": parent_id,
            "character_id": character_id,
            "section": label,
            "text": content,
            "word_count": word_count,
            "embedding_signature": signature,
            "updated_at": time.time(),
        }
        parent_count += 1

        base_metadata = {
            "character_id": character_id,
            "parent_id": parent_id,
            "section": label,
            "doc_type": "lore_child",
        }

        if word_count <= settings.lore_chunk_threshold_words:
            # Short section: the whole parent is also its own single child.
            child_docs.append(Document(page_content=content, metadata=base_metadata))
            child_ids.append(f"{parent_id}_c0")
        else:
            children = child_splitter.split_text(content)
            for j, child_text in enumerate(children):
                child_docs.append(Document(page_content=child_text, metadata=dict(base_metadata)))
                child_ids.append(f"{parent_id}_c{j}")

    # Write Chroma first, parent store second: if embedding/Chroma fails
    # (e.g. the embedding model isn't pulled locally), the previous
    # parent_store.json is left untouched rather than ending up with
    # parent entries whose child chunks don't actually exist (plan §5.2
    # edge case: "no partially-written parent store").
    if child_docs:
        lore_vectorstore.add_documents(child_docs, ids=child_ids)

    _save_parent_store(parent_store)

    logger.info(
        "Ingested character '%s': %d parent section(s), %d child chunk(s).",
        character_id,
        parent_count,
        len(child_docs),
    )


def ingest_all() -> None:
    character_ids = character_registry.ids()
    if not character_ids:
        logger.warning("No characters found in the registry — nothing to ingest.")
        return
    for character_id in character_ids:
        try:
            ingest_character(character_id)
        except Exception:
            logger.exception("Failed to ingest character '%s'", character_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest character lore documents into the lore store.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--character", help="Character id to ingest (must exist in characters.json)")
    group.add_argument("--all", action="store_true", help="Ingest every character in the registry")
    args = parser.parse_args()

    if args.all:
        ingest_all()
    else:
        ingest_character(args.character)


if __name__ == "__main__":
    main()
