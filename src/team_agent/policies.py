"""制度检索：把 Markdown 按章节切块，用词项打分返回可引用的片段。

不引入向量库：制度文件很小，关键词+bigram 打分足够可解释、可测试。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z0-9_]+")
_DOC_ID = re.compile(r"POL-[A-Z]+-\d+")


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    found = set(_WORD.findall(lowered))
    for run in _CJK.findall(lowered):
        if len(run) == 1:
            found.add(run)
        else:
            found.update(run[index : index + 2] for index in range(len(run) - 1))
    return found


def _count_terms(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for term in _terms(text):
        counts[term] = text.lower().count(term)
    return counts


@dataclass(frozen=True, slots=True)
class PolicyChunk:
    doc_id: str
    title: str
    section: str
    content: str
    path: str
    term_counts: dict[str, int]
    title_terms: frozenset[str]
    section_terms: frozenset[str]
    version: str = "未标注"
    status: str = "现行"

    def to_result(self, score: float, snippet: str) -> dict[str, object]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "section": self.section,
            "version": self.version,
            "status": self.status,
            "citation": f"{self.title} · {self.section}",
            "snippet": snippet,
            "score": round(score, 3),
            "source": self.path,
        }


def _metadata(text: str) -> tuple[str, str]:
    version = "未标注"
    status = "现行"
    for line in text.splitlines()[:8]:
        if not line.strip().startswith(">"):
            continue
        match = re.search(r"版本[：:]\s*([^\s　|]+)", line)
        if match:
            version = match.group(1).strip("*")
        state = re.search(r"状态[：:]\s*([^　|]+)", line)
        if state:
            status = state.group(1).strip("* ")
        elif "废止" in line:
            status = "已废止"
    head = text[:800]
    if "已废止" in head:
        status = "已废止"
    elif "未审核" in head:
        status = "未审核"
    return version, status


def _split_chunks(path: Path) -> list[PolicyChunk]:
    text = path.read_text(encoding="utf-8")
    doc_match = _DOC_ID.search(text)
    doc_id = doc_match.group(0) if doc_match else path.stem
    version, status = _metadata(text)

    title = path.stem
    section = "概述"
    buffer: list[str] = []
    chunks: list[tuple[str, str, str]] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            chunks.append((section, body, title))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("# "):
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            flush()
            buffer = []
            section = line[3:].strip()
            continue
        if line.startswith(">"):
            continue
        buffer.append(raw_line)
    flush()

    result: list[PolicyChunk] = []
    for section_name, body, doc_title in chunks:
        result.append(
            PolicyChunk(
                doc_id=doc_id,
                title=doc_title,
                section=section_name,
                content=body,
                path=path.name,
                term_counts=_count_terms(body),
                title_terms=frozenset(_terms(doc_title)),
                section_terms=frozenset(_terms(section_name)),
                version=version,
                status=status,
            )
        )
    return result


class PolicyIndex:
    def __init__(self, chunks: list[PolicyChunk]) -> None:
        self.chunks = chunks

    @classmethod
    def from_dir(cls, policy_dir: Path | str) -> PolicyIndex:
        root = Path(policy_dir)
        if not root.is_dir():
            raise FileNotFoundError(f"制度目录不存在：{root}")
        chunks: list[PolicyChunk] = []
        for path in sorted(root.glob("*.md")):
            chunks.extend(_split_chunks(path))
        return cls(chunks)

    def search(self, query: str, limit: int = 3) -> list[dict[str, object]]:
        query_terms = _terms(query)
        if not query_terms:
            return []

        scored: list[tuple[float, PolicyChunk]] = []
        for chunk in self.chunks:
            score = 0.0
            for term in query_terms:
                if term in chunk.term_counts:
                    score += 1.0 + math.log1p(chunk.term_counts[term])
                if term in chunk.title_terms:
                    score += 3.0
                if term in chunk.section_terms:
                    score += 2.0
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda pair: (-pair[0], pair[1].doc_id, pair[1].section))
        return [
            chunk.to_result(score, _snippet(chunk.content, query_terms)) for score, chunk in scored[: max(1, limit)]
        ]


def _snippet(content: str, terms: set[str], width: int = 140) -> str:
    text = " ".join(content.split())
    lowered = text.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(positions) - 30) if positions else 0
    snippet = text[start : start + width]
    if start > 0:
        snippet = "…" + snippet
    if start + width < len(text):
        snippet += "…"
    return snippet
