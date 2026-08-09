from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from alarmops.models import Citation

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]+")
UNTRUSTED_PATTERNS = ("ignore previous", "system prompt", "reveal secrets", "developer message")
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.casefold())


class DenseEmbedder(Protocol):
    """Small interface that keeps retrieval testable without downloading a model in unit tests."""

    model_name: str
    dimension: int

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class MiniLmEmbedder:
    """Local Hugging Face MiniLM encoder using lightweight ONNX Runtime."""

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL, device: str = "cpu") -> None:
        if device.casefold() != "cpu":
            raise ValueError("this project installs CPU FastEmbed; set RAG_EMBEDDING_DEVICE=cpu")
        try:
            from fastembed import TextEmbedding
        except ImportError as error:  # pragma: no cover - exercised by installation checks
            raise RuntimeError(
                "MiniLM retrieval requires fastembed. "
                'Run: python -m pip install -e ".[dev,notebook]"'
            ) from error

        self.model_name = model_name
        supported = {
            item["model"]: int(item["dim"]) for item in TextEmbedding.list_supported_models()
        }
        if model_name not in supported:
            raise ValueError(f"FastEmbed does not support configured model: {model_name}")
        self.dimension = supported[model_name]
        self._model = TextEmbedding(
            model_name=model_name,
            providers=["CPUExecutionProvider"],
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        result: list[list[float]] = []
        for row in self._model.embed(texts):
            vector = [float(value) for value in row]
            norm = math.sqrt(sum(value**2 for value in vector))
            result.append([value / max(norm, 1e-12) for value in vector])
        return result


@dataclass
class Chunk:
    document_id: str
    title: str
    section: str
    source_path: str
    text: str
    term_counts: dict[str, int]
    length: int
    embedding: list[float] = field(default_factory=list)

    @property
    def embedding_text(self) -> str:
        return f"{self.title}. {self.section}. {self.text}"


class HybridRagIndex:
    """Hybrid BM25 + local MiniLM cosine index for small, trusted runbooks.

    ``embedding_model_name=None`` keeps a sparse-only mode for fast unit tests. The
    application passes the configured Hugging Face model and therefore uses dense
    semantic retrieval in normal local and deployed runs.
    """

    def __init__(
        self,
        chunks: list[Chunk],
        embedder: DenseEmbedder | None = None,
        semantic_weight: float = 0.55,
        semantic_threshold: float = 0.28,
    ) -> None:
        self.chunks = chunks
        self.embedder = embedder
        self.semantic_weight = semantic_weight
        self.semantic_threshold = semantic_threshold
        self.document_frequency = Counter(
            term for chunk in chunks for term in set(chunk.term_counts)
        )
        self.average_length = sum(chunk.length for chunk in chunks) / max(len(chunks), 1)

    @classmethod
    def build(
        cls,
        document_path: Path,
        embedding_model_name: str | None = None,
        embedding_device: str = "cpu",
        semantic_weight: float = 0.55,
        semantic_threshold: float = 0.28,
        embedder: DenseEmbedder | None = None,
    ) -> HybridRagIndex:
        chunks = cls._chunk_documents(document_path)
        active_embedder = embedder
        if active_embedder is None and embedding_model_name:
            active_embedder = MiniLmEmbedder(embedding_model_name, embedding_device)
        if active_embedder and chunks:
            vectors = active_embedder.encode([chunk.embedding_text for chunk in chunks])
            if len(vectors) != len(chunks):
                raise ValueError("embedding count does not match document chunk count")
            for chunk, vector in zip(chunks, vectors, strict=True):
                chunk.embedding = vector
        return cls(chunks, active_embedder, semantic_weight, semantic_threshold)

    @staticmethod
    def _chunk_documents(document_path: Path) -> list[Chunk]:
        chunks: list[Chunk] = []

        def append_buffer(
            path: Path,
            title: str,
            section: str,
            buffer: list[str],
        ) -> None:
            if not buffer:
                return
            text = " ".join(buffer).strip()
            words = tokenize(text)
            if words:
                chunks.append(
                    Chunk(
                        path.stem,
                        title,
                        section,
                        str(path),
                        text,
                        dict(Counter(words)),
                        len(words),
                    )
                )
            buffer.clear()

        for path in sorted(document_path.glob("*.md")):
            raw = path.read_text(encoding="utf-8")
            if any(pattern in raw.casefold() for pattern in UNTRUSTED_PATTERNS):
                continue
            title = path.stem.replace("_", " ").title()
            section = "Overview"
            buffer: list[str] = []

            for line in raw.splitlines():
                if line.startswith("#"):
                    append_buffer(path, title, section, buffer)
                    section = line.lstrip("# ").strip()
                    if line.startswith("# "):
                        title = section
                elif line.strip():
                    buffer.append(line.strip())
            append_buffer(path, title, section, buffer)
        return chunks

    def save(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "embedding_model": self.embedder.model_name if self.embedder else None,
            "embedding_dimension": self.embedder.dimension if self.embedder else 0,
            "semantic_weight": self.semantic_weight,
            "semantic_threshold": self.semantic_threshold,
            "chunks": [asdict(chunk) for chunk in self.chunks],
        }
        destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(
        cls,
        source: Path,
        embedding_device: str = "cpu",
        embedder: DenseEmbedder | None = None,
    ) -> HybridRagIndex:
        payload = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(payload, list):  # Backward compatibility with the original sparse index.
            return cls([Chunk(**item) for item in payload])
        model_name = payload.get("embedding_model")
        active_embedder = embedder
        if active_embedder is None and model_name:
            active_embedder = MiniLmEmbedder(model_name, embedding_device)
        return cls(
            [Chunk(**item) for item in payload["chunks"]],
            active_embedder,
            float(payload.get("semantic_weight", 0.55)),
            float(payload.get("semantic_threshold", 0.28)),
        )

    def _bm25(self, chunk: Chunk, query_terms: Counter[str]) -> float:
        total = max(len(self.chunks), 1)
        score = 0.0
        for term in query_terms:
            df = self.document_frequency.get(term, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            frequency = chunk.term_counts.get(term, 0)
            denominator = frequency + 1.5 * (
                1 - 0.75 + 0.75 * chunk.length / max(self.average_length, 1)
            )
            score += idf * frequency * 2.5 / max(denominator, 0.001)
        return score

    def _tfidf_cosine(self, chunk: Chunk, query_terms: Counter[str]) -> float:
        total = max(len(self.chunks), 1)
        dot = query_norm = chunk_norm = 0.0
        for term, query_count in query_terms.items():
            df = self.document_frequency.get(term, 0)
            idf = math.log((total + 1) / (df + 1)) + 1
            chunk_weight = chunk.term_counts.get(term, 0) * idf
            query_weight = query_count * idf
            dot += chunk_weight * query_weight
            query_norm += query_weight**2
        for term, frequency in chunk.term_counts.items():
            df = self.document_frequency.get(term, 0)
            idf = math.log((total + 1) / (df + 1)) + 1
            chunk_norm += (frequency * idf) ** 2
        return dot / max(math.sqrt(query_norm * chunk_norm), 0.001)

    @staticmethod
    def _dot(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            return 0.0
        return sum(a * b for a, b in zip(left, right, strict=True))

    def search(self, query: str, limit: int = 4) -> list[Citation]:
        query_terms = Counter(tokenize(query))
        if not query_terms or not self.chunks:
            return []

        bm25_scores = [self._bm25(chunk, query_terms) for chunk in self.chunks]
        max_bm25 = max(bm25_scores, default=0.0)
        query_embedding: list[float] = []
        if self.embedder and any(chunk.embedding for chunk in self.chunks):
            query_embedding = self.embedder.encode([query])[0]

        scored: list[tuple[float, Chunk]] = []
        for chunk, bm25 in zip(self.chunks, bm25_scores, strict=True):
            if query_embedding:
                semantic = max(-1.0, min(1.0, self._dot(query_embedding, chunk.embedding)))
                if bm25 <= 0 and semantic < self.semantic_threshold:
                    continue
                lexical = bm25 / max(max_bm25, 0.001)
                score = (1 - self.semantic_weight) * lexical + self.semantic_weight * max(
                    semantic, 0.0
                )
            else:
                cosine = self._tfidf_cosine(chunk, query_terms)
                score = 0.65 * bm25 + 0.35 * cosine
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            Citation(
                citation_id=f"DOC-{index}",
                document_id=chunk.document_id,
                title=chunk.title,
                section=chunk.section,
                score=round(score, 4),
                excerpt=chunk.text[:360],
                source_path=chunk.source_path,
            )
            for index, (score, chunk) in enumerate(scored[:limit], 1)
        ]

    def diagnostics(self) -> dict[str, Any]:
        dense = self.embedder is not None and any(chunk.embedding for chunk in self.chunks)
        return {
            "documents": len({chunk.document_id for chunk in self.chunks}),
            "chunks": len(self.chunks),
            "method": "BM25 + Hugging Face MiniLM cosine" if dense else "BM25 + TF-IDF cosine",
            "embedding_model": self.embedder.model_name if self.embedder else None,
            "embedding_dimension": self.embedder.dimension if self.embedder else 0,
            "semantic_weight": self.semantic_weight if dense else 0.0,
            "semantic_threshold": self.semantic_threshold if dense else None,
            "embedding_api_required": False,
        }


__all__ = [
    "DEFAULT_EMBEDDING_MODEL",
    "DenseEmbedder",
    "HybridRagIndex",
    "MiniLmEmbedder",
]
