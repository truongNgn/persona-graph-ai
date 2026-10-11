"""LangChain Chroma memory store + BM25 mirror for hybrid retrieval."""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings
from langchain_core.documents import Document

from app.config import settings
from app.bm25_store import bm25_store, BM25SearchResult

logger = logging.getLogger(__name__)


@dataclass
class HybridRetrievalMetrics:
    query_length: int
    top_k: int
    route: str = "hybrid"
    facts_count: int = 0
    dense_count: int = 0
    sparse_count: int = 0
    fused_count: int = 0
    bm25_store_size: int = 0
    facts_latency_ms: int = 0
    dense_latency_ms: int = 0
    sparse_latency_ms: int = 0
    total_latency_ms: int = 0
    skipped_turn_retrieval: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_length": self.query_length,
            "top_k": self.top_k,
            "route": self.route,
            "facts_count": self.facts_count,
            "dense_count": self.dense_count,
            "sparse_count": self.sparse_count,
            "fused_count": self.fused_count,
            "bm25_store_size": self.bm25_store_size,
            "facts_latency_ms": self.facts_latency_ms,
            "dense_latency_ms": self.dense_latency_ms,
            "sparse_latency_ms": self.sparse_latency_ms,
            "total_latency_ms": self.total_latency_ms,
            "skipped_turn_retrieval": self.skipped_turn_retrieval,
            "errors": self.errors,
        }


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)

_MEMORY_INTENT_RE = re.compile(
    r"\b("
    r"nhớ|nho|đã nói|da noi|từng nói|tung noi|lần trước|lan truoc|trước đó|truoc do"
    r"|hôm qua|hom qua|vừa rồi|vua roi|cuộc trò chuyện|cuoc tro chuyen"
    r"|tên tôi|ten toi|tôi tên|toi ten|sở thích|so thich|công việc|cong viec"
    r"|nghề|nghe|địa chỉ|dia chi|ở đâu|o dau|tiếp tục|tiep tuc|tiếp đi|tiep di"
    r"|remember|recall|previous|earlier|last time|yesterday|continue|my name"
    r"|my job|my work|my interest|my hobby|where do i|what did i"
    r")\b",
    re.IGNORECASE,
)

_SMALL_TALK_RE = re.compile(
    r"^\s*("
    r"hi|hello|hey|yo|alo|chào|chao|xin chào|xin chao|ok|okay|uhm|ừ|ừm|ờ"
    r"|yes|no|thanks|thank you|cảm ơn|cam on|haha|hehe|lol|good morning"
    r"|good afternoon|good evening"
    r")[\s.!?。？！]*$",
    re.IGNORECASE,
)

_REFERENCE_RE = re.compile(
    r"\b(it|that|this|those|them|there|above|before|nó|đó|kia|cái đó|vấn đề đó|ý đó)\b",
    re.IGNORECASE,
)

def _get_embeddings():
    # embedding_provider is intentionally its own setting, independent from
    # llm_provider (docs/MODEL_MANAGEMENT_PLAN.md §2.12) — swapping the chat
    # model must not silently swap the vector space of everything already
    # embedded in Chroma.
    provider = settings.embedding_provider.lower().strip()
    if provider == "gemini":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        logger.info("Initializing GoogleGenerativeAIEmbeddings (text-embedding-004)")
        return GoogleGenerativeAIEmbeddings(
            model="models/text-embedding-004",
            google_api_key=settings.gemini_api_key,
        )
    logger.info("Initializing OllamaEmbeddings (%s)", settings.embedding_model)
    return OllamaEmbeddings(
        model=settings.embedding_model,
        base_url=settings.ollama_host,
    )

embeddings = _get_embeddings()


def embedding_signature() -> str:
    """Stable id for the active embedding config, e.g. 'ollama:nomic-embed-text'.

    Consumers that persist embeddings alongside metadata (lore_ingest.py,
    lore_store.py) should stamp this value and warn on mismatch at load time
    — a silently different embedding_provider/embedding_model makes prior
    vectors unsearchable without any error.
    """
    provider = settings.embedding_provider.lower().strip()
    model = "text-embedding-004" if provider == "gemini" else settings.embedding_model
    return f"{provider}:{model}"

vectorstore = Chroma(
    collection_name="chat_memories",
    embedding_function=embeddings,
    persist_directory=settings.chroma_path,
)


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def _mirror_to_bm25(doc_id: str, text: str, metadata: dict) -> None:
    """Mirror a document to the BM25 store immediately after ChromaDB write."""
    try:
        bm25_store.add_document(doc_id, text, metadata)
        logger.debug("BM25 mirror: %s", text[:60])
    except Exception as e:
        logger.warning("BM25 mirror failed (non-fatal): %s", e)


async def store_turn(
    user_id: str, session_id: str, character_id: str, role: str, text: str, emotion: str
) -> None:
    if not settings.memory_enabled:
        return

    content = f"[{emotion}] {role}: {text}"
    doc_id = f"{user_id}_{character_id}_{session_id}_{role}_{int(time.time() * 1000)}"
    metadata = {
        "user_id": user_id,
        "character_id": character_id,
        "session_id": session_id,
        "timestamp": time.time(),
        "role": role,
        "doc_type": "turn",
    }
    doc = Document(page_content=content, metadata=metadata)
    try:
        await vectorstore.aadd_documents([doc])
        _mirror_to_bm25(doc_id, content, metadata)
        logger.debug("Stored memory for %s: %s", role, text[:40])
    except Exception as e:
        logger.warning("Memory store failed: %s", e)


async def store_fact(user_id: str, fact_type: str, fact_value: str) -> None:
    """
    Store a GLOBAL structured fact (e.g. user's name, job) — shared across all
    characters, since these are true about the user regardless of which
    character they're roleplaying with.
    """
    if not settings.memory_enabled:
        return

    content = f"[FACT] {fact_type}: {fact_value}"
    try:
        # Deduplicate: skip if identical fact already stored
        existing = await vectorstore.asimilarity_search(
            query=content,
            k=1,
            filter={"$and": [{"user_id": user_id}, {"doc_type": "fact"}, {"fact_type": fact_type}]},
        )
        if existing and existing[0].page_content == content:
            logger.debug("Fact already stored: %s=%s", fact_type, fact_value)
            return

        doc_id = f"{user_id}_fact_{fact_type}"
        metadata = {
            "user_id": user_id,
            "timestamp": time.time(),
            "doc_type": "fact",
            "fact_type": fact_type,
        }
        doc = Document(page_content=content, metadata=metadata)
        await vectorstore.aadd_documents([doc])
        _mirror_to_bm25(doc_id, content, metadata)
        logger.info("Stored fact [%s] = %s for user %s", fact_type, fact_value, user_id)
    except Exception as e:
        logger.warning("Fact store failed: %s", e)


async def store_character_fact(user_id: str, character_id: str, fact_type: str, fact_value: str) -> None:
    """
    Store a fact scoped to one (user, character) pair — e.g. something the
    user only told this specific character. Kept separate from store_fact()
    so it never leaks across characters during roleplay.
    """
    if not settings.memory_enabled:
        return

    content = f"[FACT] {fact_type}: {fact_value}"
    try:
        existing = await vectorstore.asimilarity_search(
            query=content,
            k=1,
            filter={
                "$and": [
                    {"user_id": user_id},
                    {"character_id": character_id},
                    {"doc_type": "character_fact"},
                    {"fact_type": fact_type},
                ]
            },
        )
        if existing and existing[0].page_content == content:
            return

        doc_id = f"{user_id}_{character_id}_fact_{fact_type}"
        metadata = {
            "user_id": user_id,
            "character_id": character_id,
            "timestamp": time.time(),
            "doc_type": "character_fact",
            "fact_type": fact_type,
        }
        doc = Document(page_content=content, metadata=metadata)
        await vectorstore.aadd_documents([doc])
        _mirror_to_bm25(doc_id, content, metadata)
        logger.info("Stored character fact [%s] = %s for %s/%s", fact_type, fact_value, user_id, character_id)
    except Exception as e:
        logger.warning("Character fact store failed: %s", e)


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def should_retrieve_turn_memories(query: str) -> bool:
    """
    Decide whether a turn needs expensive Chroma/BM25 conversation retrieval.

    Structured facts can still be loaded cheaply by metadata. This only gates
    dense/sparse retrieval of prior conversation turns.
    """
    if not settings.memory_fast_path_enabled:
        return True

    q = query.strip()
    if not q:
        return False
    if _MEMORY_INTENT_RE.search(q):
        return True
    if _SMALL_TALK_RE.match(q):
        return False
    if len(q) <= 24:
        return False
    return True


async def retrieve_facts(user_id: str, character_id: str | None = None) -> str | None:
    """
    Retrieve structured facts about the user without embedding search.

    Always includes global facts (true regardless of character). If
    character_id is given, also includes facts scoped to that character —
    never facts scoped to a *different* character (keeps roleplay isolated).
    """
    if not settings.memory_enabled:
        return None
    try:
        result = await asyncio.to_thread(
            vectorstore.get,
            where={"$and": [{"user_id": user_id}, {"doc_type": "fact"}]},
            limit=20,
            include=["documents"],
        )
        lines = [str(doc) for doc in (result.get("documents") or []) if doc]

        if character_id:
            char_result = await asyncio.to_thread(
                vectorstore.get,
                where={
                    "$and": [
                        {"user_id": user_id},
                        {"character_id": character_id},
                        {"doc_type": "character_fact"},
                    ]
                },
                limit=20,
                include=["documents"],
            )
            lines.extend(str(doc) for doc in (char_result.get("documents") or []) if doc)

        if not lines:
            return None
        return "Known facts about the user:\n" + "\n".join(f"- {l}" for l in lines)
    except Exception as e:
        logger.warning("Fact retrieval failed: %s", e)
        return None


async def retrieve_memories(user_id: str, character_id: str, query: str, k: int = 5) -> str | None:
    """Dense-only retrieval (ChromaDB). Kept for backward compatibility."""
    if not settings.memory_enabled:
        return None

    parts: list[str] = []

    facts = await retrieve_facts(user_id, character_id)
    if facts:
        parts.append(facts)

    if not should_retrieve_turn_memories(query):
        logger.debug("retrieve_memories: fast-path skipped turn retrieval for %r", query[:60])
        return "\n\n".join(parts) if parts else None

    try:
        docs = await vectorstore.asimilarity_search(
            query=query,
            k=k,
            filter={"$and": [{"user_id": user_id}, {"character_id": character_id}]},
        )
        turn_docs = [d for d in docs if d.metadata.get("doc_type") not in ("fact", "character_fact")]
        if turn_docs:
            turns = "\n".join(f"- {doc.page_content}" for doc in turn_docs)
            parts.append("Relevant past conversation:\n" + turns)
    except Exception as e:
        logger.warning("Memory retrieval failed: %s", e)

    return "\n\n".join(parts) if parts else None


def _rrf_fusion(
    dense_results: list[Document],
    sparse_results: list[BM25SearchResult],
    k: int | None = None,
    dense_weight: float | None = None,
    sparse_weight: float | None = None,
    top_k: int = 5,
) -> list[str]:
    """
    Reciprocal Rank Fusion: combine dense (ChromaDB) and sparse (BM25) results.

    sparse_weight > dense_weight because dense search already handles semantics
    well; BM25 bonus is the marginal gain for exact keyword matches.

    Returns a list of page_content strings ready for prompt injection.
    """
    rrf_k = settings.memory_rrf_k if k is None else k
    dense_score_weight = settings.memory_dense_weight if dense_weight is None else dense_weight
    sparse_score_weight = settings.memory_sparse_weight if sparse_weight is None else sparse_weight
    scores: dict[str, float] = {}
    texts: dict[str, str] = {}

    for rank, doc in enumerate(dense_results, start=1):
        key = doc.page_content
        scores[key] = scores.get(key, 0.0) + dense_score_weight / (rrf_k + rank)
        texts[key] = doc.page_content

    for rank, result in enumerate(sparse_results, start=1):
        key = result.text
        scores[key] = scores.get(key, 0.0) + sparse_score_weight / (rrf_k + rank)
        texts[key] = result.text

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [texts[key] for key, _ in ranked[:top_k]]


async def hybrid_retrieve(user_id: str, character_id: str, query: str, k: int = 5) -> str | None:
    context, _ = await hybrid_retrieve_with_metrics(user_id, character_id, query, k)
    return context


async def hybrid_retrieve_with_metrics(
    user_id: str,
    character_id: str,
    query: str,
    k: int = 5,
) -> tuple[str | None, HybridRetrievalMetrics]:
    """
    Hybrid retrieval: BM25 sparse + ChromaDB dense, fused via RRF.

    Facts are always prepended (not fused — they are retrieved by filter,
    not by relevance, so ranking doesn't apply). Turn retrieval is scoped to
    (user_id, character_id) so memories don't leak across characters.
    """
    total_started_at = time.perf_counter()
    metrics = HybridRetrievalMetrics(query_length=len(query), top_k=k)
    if not settings.memory_enabled:
        metrics.route = "disabled"
        metrics.total_latency_ms = _elapsed_ms(total_started_at)
        return None, metrics

    parts: list[str] = []

    # 1. Always fetch structured facts first (global + character-scoped)
    facts_started_at = time.perf_counter()
    facts = await retrieve_facts(user_id, character_id)
    metrics.facts_latency_ms = _elapsed_ms(facts_started_at)
    if facts:
        metrics.facts_count = sum(1 for line in facts.splitlines() if line.startswith("- "))
        parts.append(facts)

    if not should_retrieve_turn_memories(query):
        metrics.route = "facts_only"
        metrics.skipped_turn_retrieval = True
        metrics.total_latency_ms = _elapsed_ms(total_started_at)
        logger.debug("hybrid_retrieve: fast-path skipped turn retrieval for %r", query[:60])
        return ("\n\n".join(parts) if parts else None), metrics

    # 2. Dense retrieval — over-fetch for better RRF coverage
    dense_docs: list[Document] = []
    dense_started_at = time.perf_counter()
    try:
        dense_docs = await vectorstore.asimilarity_search(
            query=query,
            k=k * settings.memory_dense_overfetch_multiplier,
            filter={"$and": [{"user_id": user_id}, {"character_id": character_id}]},
        )
        # Exclude fact documents — already handled above
        dense_docs = [d for d in dense_docs if d.metadata.get("doc_type") not in ("fact", "character_fact")]
        metrics.dense_count = len(dense_docs)
    except Exception as e:
        logger.warning("Dense retrieval failed: %s", e)
        metrics.errors.append(f"dense: {e}")
    finally:
        metrics.dense_latency_ms = _elapsed_ms(dense_started_at)

    # 3. Sparse (BM25) retrieval — filter by user_id + character_id in metadata
    sparse_results: list[BM25SearchResult] = []
    sparse_started_at = time.perf_counter()
    try:
        all_sparse = bm25_store.search(query, top_k=k * 2)
        sparse_results = [
            r for r in all_sparse
            if r.metadata.get("user_id") == user_id
            and r.metadata.get("character_id") == character_id
            and r.metadata.get("doc_type") not in ("fact", "character_fact")
        ]
        metrics.sparse_count = len(sparse_results)
    except Exception as e:
        logger.warning("BM25 retrieval failed (non-fatal): %s", e)
        metrics.errors.append(f"sparse: {e}")
    finally:
        metrics.sparse_latency_ms = _elapsed_ms(sparse_started_at)

    # 4. Fuse results
    if not dense_docs and not sparse_results:
        metrics.route = "facts_only" if parts else "empty"
        metrics.total_latency_ms = _elapsed_ms(total_started_at)
        metrics.bm25_store_size = bm25_store.size()
        logger.info("retrieval_metrics=%s", metrics.to_dict())
        return ("\n\n".join(parts) if parts else None), metrics

    fused = _rrf_fusion(dense_docs, sparse_results, top_k=k)
    metrics.fused_count = len(fused)
    if fused:
        turns = "\n".join(f"- {text}" for text in fused)
        parts.append("Relevant past conversation:\n" + turns)

    metrics.bm25_store_size = bm25_store.size()
    metrics.total_latency_ms = _elapsed_ms(total_started_at)
    logger.info("retrieval_metrics=%s", metrics.to_dict())

    return ("\n\n".join(parts) if parts else None), metrics
