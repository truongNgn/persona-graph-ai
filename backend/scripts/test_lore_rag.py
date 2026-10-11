"""Automated verification for the character lore RAG pipeline (Stage 5 of
docs/RAG_character_roleplay_implementation_plan.md).

Covers §5.1 (ingest the real luna/kai data, verify parent store + Chroma +
cross-character isolation + idempotency) and §5.2 edge cases, the latter
using a throwaway synthetic character (`_edgecase_test`) so word-threshold
boundary tests don't depend on mutating the real character files.

This is a live integration script, not a unit test: it requires a running
Ollama with the embedding model in EMBEDDING_MODEL pulled (default
nomic-embed-text) and writes to the real Chroma store at settings.chroma_path
and the real lore_data/parent_store.json. It cleans up everything it adds
under `_edgecase_test`; it does not touch luna/kai data (re-ingesting them at
the start is intentional and safe — Stage 3 verified this is idempotent).

Usage:
    cd backend
    python scripts/test_lore_rag.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app import lore_ingest
from app.character_registry import Character, character_registry
from app.config import settings
from app.lore_store import lore_vectorstore, retrieve_character_context

EDGE_CHARACTER_ID = "_edgecase_test"


def _check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: FAILED {detail}".rstrip())
    print(f"{label}: ok {detail}".rstrip())


def _word_text(n: int) -> str:
    """n-word string with an exact, easily-verifiable word count."""
    return " ".join(["từ"] * n)


# ---------------------------------------------------------------------------
# §5.1 — real luna/kai data: parent store, Chroma, isolation, idempotency
# ---------------------------------------------------------------------------

async def check_ingest_and_isolation() -> None:
    lore_ingest.ingest_all()

    parent_store = lore_ingest._load_parent_store()
    luna_parents = [p for p in parent_store.values() if p["character_id"] == "luna"]
    kai_parents = [p for p in parent_store.values() if p["character_id"] == "kai"]
    _check("parent_store_has_luna", len(luna_parents) > 0, f"count={len(luna_parents)}")
    _check("parent_store_has_kai", len(kai_parents) > 0, f"count={len(kai_parents)}")
    _check(
        "parent_id_prefix_correct",
        all(p["parent_id"].startswith("luna_") for p in luna_parents)
        and all(p["parent_id"].startswith("kai_") for p in kai_parents),
    )

    total = lore_vectorstore._collection.count()
    _check("chroma_has_documents", total > 0, f"count={total}")

    # Isolation is enforced by the character_id metadata filter at the Chroma
    # query level — check that directly rather than sniffing rendered text
    # for the word "Kai" (luna's own "Relationships / Kai" section legitimately
    # contains that word, so a text-content check would be a wrong test).
    child_docs = await lore_vectorstore.asimilarity_search(
        "Kai dam me thien van hoc, thich ngam sao", k=10, filter={"character_id": "luna"}
    )
    leaked = [d.metadata["parent_id"] for d in child_docs if not d.metadata["parent_id"].startswith("luna_")]
    _check("cross_query_no_foreign_parent_id", len(leaked) == 0, f"leaked={leaked}")

    context = await retrieve_character_context("luna", "Kai dam me thien van hoc, thich ngam sao")
    _check("cross_query_context_nonempty", len(context) > 0)

    count_before_reingest = lore_vectorstore._collection.count()
    lore_ingest.ingest_character("luna")
    count_after_reingest = lore_vectorstore._collection.count()
    _check(
        "reingest_is_idempotent",
        count_after_reingest == count_before_reingest,
        f"before={count_before_reingest} after={count_after_reingest}",
    )


# ---------------------------------------------------------------------------
# §5.2 — edge cases via a throwaway synthetic character
# ---------------------------------------------------------------------------

async def check_edge_cases() -> None:
    threshold = settings.lore_chunk_threshold_words

    md_content = (
        "# NoSubsection\n"
        "Just prose, no ## subheadings in this section at all.\n\n"
        f"# BelowThreshold\n{_word_text(threshold - 1)}\n\n"
        f"# AtThreshold\n{_word_text(threshold)}\n\n"
        f"# AboveThreshold\n{_word_text(threshold + 500)}\n"
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        md_path = Path(tmp_dir) / f"{EDGE_CHARACTER_ID}.md"
        md_path.write_text(md_content, encoding="utf-8")

        # Inject a throwaway character into the live registry for this run
        # only — brain_path as an absolute Path resolves correctly through
        # ingest_character()'s `PROJECT_ROOT / brain_path` join (pathlib
        # discards the left side when the right side is already absolute).
        character_registry._characters[EDGE_CHARACTER_ID] = Character(
            id=EDGE_CHARACTER_ID,
            display_name="Edge Case Test",
            brain_path=str(md_path),
        )

        try:
            lore_ingest.ingest_character(EDGE_CHARACTER_ID)

            parent_store = lore_ingest._load_parent_store()
            edge_parents = {
                p["section"]: p for p in parent_store.values() if p["character_id"] == EDGE_CHARACTER_ID
            }

            _check("edge_no_subsection_present", "NoSubsection" in edge_parents)
            _check("edge_below_threshold_present", "BelowThreshold" in edge_parents)
            _check("edge_at_threshold_present", "AtThreshold" in edge_parents)
            _check("edge_above_threshold_present", "AboveThreshold" in edge_parents)

            _check(
                "edge_below_threshold_word_count",
                edge_parents["BelowThreshold"]["word_count"] == threshold - 1,
            )
            _check(
                "edge_at_threshold_word_count",
                edge_parents["AtThreshold"]["word_count"] == threshold,
            )

            below_children = lore_vectorstore.get(where={"parent_id": edge_parents["BelowThreshold"]["parent_id"]})
            at_children = lore_vectorstore.get(where={"parent_id": edge_parents["AtThreshold"]["parent_id"]})
            above_children = lore_vectorstore.get(where={"parent_id": edge_parents["AboveThreshold"]["parent_id"]})

            _check(
                "edge_below_threshold_single_child",
                len(below_children["ids"]) == 1,
                f"count={len(below_children['ids'])}",
            )
            _check(
                "edge_at_threshold_off_by_one_single_child",
                len(at_children["ids"]) == 1,
                f"count={len(at_children['ids'])} (threshold condition must be word_count <= threshold, not <)",
            )
            _check(
                "edge_above_threshold_triggers_tier2_split",
                len(above_children["ids"]) > 1,
                f"count={len(above_children['ids'])}",
            )

            # Cross-check against the same splitter lore_ingest.py actually
            # uses, so this doesn't just assert "> 1" but the right count.
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=settings.lore_chunk_size_chars,
                chunk_overlap=settings.lore_chunk_overlap_chars,
                separators=["\n## ", "\n\n", "\n", ". ", " "],
            )
            expected_children = len(splitter.split_text(edge_parents["AboveThreshold"]["text"]))
            _check(
                "edge_above_threshold_child_count_matches_splitter",
                len(above_children["ids"]) == expected_children,
                f"actual={len(above_children['ids'])} expected={expected_children}",
            )

            # Ingest failure must not partially write the parent store (the
            # ordering bug caught during Stage 3's first real run). Simulate
            # a Chroma/embedding failure by breaking add_documents rather
            # than depending on Ollama's specific down-state behavior.
            parent_store_before_failure = lore_ingest._load_parent_store()
            original_add_documents = lore_vectorstore.add_documents

            def _boom(*_args, **_kwargs):
                raise RuntimeError("simulated embedding/Chroma failure")

            lore_vectorstore.add_documents = _boom
            try:
                raised = False
                try:
                    lore_ingest.ingest_character(EDGE_CHARACTER_ID)
                except Exception:
                    raised = True
                _check("ingest_failure_raises", raised)
                parent_store_after_failure = lore_ingest._load_parent_store()
                _check(
                    "ingest_failure_no_partial_parent_store_write",
                    parent_store_after_failure == parent_store_before_failure,
                )
            finally:
                lore_vectorstore.add_documents = original_add_documents
        finally:
            lore_vectorstore.delete(where={"character_id": EDGE_CHARACTER_ID})
            parent_store = lore_ingest._load_parent_store()
            cleaned = {k: v for k, v in parent_store.items() if v.get("character_id") != EDGE_CHARACTER_ID}
            lore_ingest._save_parent_store(cleaned)
            character_registry._characters.pop(EDGE_CHARACTER_ID, None)

    # No-heading fallback: whole file becomes a single section.
    no_heading_text = "Just plain prose, no headings at all in this document whatsoever."
    docs = lore_ingest._split_sections(no_heading_text, "edgecase_no_heading")
    _check("no_heading_fallback_single_section", len(docs) == 1, f"count={len(docs)}")
    _check("no_heading_fallback_content_preserved", docs[0].page_content.strip() == no_heading_text)

    # Unknown character_id must not crash retrieval — retrieve_character_context
    # returns "" and (indirectly, via persona.py's fallback) chat still works.
    context = await retrieve_character_context("does_not_exist_in_registry", "hello")
    _check("unknown_character_returns_empty_context", context == "", f"repr={context!r}")


async def main() -> None:
    await check_ingest_and_isolation()
    await check_edge_cases()
    print("test_lore_rag: all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
