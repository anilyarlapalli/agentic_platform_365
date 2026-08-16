"""Retained bytes → text → chunks. The only place a chunk is manufactured.

Before this module the platform could copy, keep and drop chunks but never
*make* one outside a seed script, so a document whose content changed had
nothing to be re-read from and quietly contributed nothing to the next build.

## Why not the engine's ingestion pipeline

``doc_pipeline.ingestion`` has a structure-aware chunker, and its
``router`` imports ``PDFParser`` at module scope, which imports PyMuPDF. Reaching
for it would pull the parser stack into the API process for the sake of splitting
markdown, and it lives in a tree this project treats as read-only reference. The
retrieval dependency floor stays small; ingestion of binary formats is a decision
to take deliberately, not one to inherit from an import.

## Extraction is only defined for formats we can actually read

``SUPPORTED_SUFFIXES`` is the authority, and ``POST /api/documents`` rejects
anything outside it at the door. Accepting a ``.pdf`` that can never be chunked
would produce a document the platform stores, lists and reports as current while
it contributes nothing retrievable — a declared capability with nothing behind
it, which is the shape of defect this codebase keeps finding.

## Chunk identity is content, not position

``canonical_id`` is ``c_<sha1:16>`` of the stripped text — the same namespace the
seed scripts use. Two consequences that matter: re-chunking an unchanged document
reproduces exactly the same ids, so a rebuild is genuinely idempotent; and a
stored eval result still names a real chunk after the corpus is rebuilt, which a
positional id would not.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

logger = logging.getLogger("platform.corpus.chunking")

SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".csv", ".html"})

# Target size in tokens. Small enough that a retrieved passage is mostly answer
# rather than context, large enough that a specification and its qualifying
# sentence ("do not exceed 95 Nm") stay in the same chunk — splitting those
# apart is how a retriever returns a figure without its caveat.
TARGET_TOKENS = 420
# Below this a trailing fragment is merged backwards instead of standing alone.
# A 12-token chunk matches a query on one incidental word and outranks the
# passage that actually answers it.
MIN_TOKENS = 60
# Carried between adjacent chunks so a sentence spanning a boundary is complete
# in at least one of them.
OVERLAP_TOKENS = 60

_ENCODING = "cl100k_base"

# A markdown/setext heading starts a new section. Everything under it inherits
# it as a breadcrumb, so a chunk taken from the middle of a document still says
# what it is about.
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass(frozen=True, slots=True)
class Chunk:
    canonical_id: str
    ordinal: int
    text: str
    heading: str | None


class UnsupportedDocument(ValueError):
    """The bytes cannot be turned into text with what is installed.

    Raised rather than returning an empty string: "no text" and "cannot read
    this" lead to different fixes, and collapsing them would report a corpus as
    complete while a whole format silently contributed nothing.
    """


def canonical_id(chunk_text: str) -> str:
    """``c_<sha1:16>`` — content-addressed, stable across rebuilds.

    The single id namespace the platform uses. Nothing derived from a position
    in a list ever crosses a boundary, which is what keeps a stored eval result
    meaningful after the corpus is rebuilt.
    """
    return "c_" + hashlib.sha1(chunk_text.strip().encode()).hexdigest()[:16]


# ── extraction ────────────────────────────────────────────────────────────


class _TextExtractor(HTMLParser):
    """Tags out, text in. Script and style bodies dropped entirely.

    Keeping ``<script>`` contents would embed minified JavaScript as though it
    were prose — it retrieves badly and costs real tokens to embed.
    """

    _SKIP = frozenset({"script", "style", "noscript", "template"})
    _BLOCK = frozenset(
        {"p", "div", "br", "li", "tr", "section", "article", "table",
         "blockquote", "pre"}
    )
    # Rendered as markdown headings rather than as plain lines, so section
    # detection downstream has one rule instead of one per format — an HTML
    # manual gets the same breadcrumbs a markdown one does.
    _HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####", "h5": "#####", "h6": "######"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._HEADINGS:
            self._parts.append(f"\n\n{self._HEADINGS[tag]} ")
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._HEADINGS:
            self._parts.append("\n\n")
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _decode(data: bytes) -> str:
    """UTF-8, then a permissive fallback.

    ``errors="replace"`` rather than a hard failure: a single bad byte in a
    500KB manual should cost that one character, not the whole document. The
    substitution is visible in the stored chunk if anyone looks.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("document is not valid UTF-8; decoding with replacement")
        return data.decode("utf-8", errors="replace")


def _csv_to_text(raw: str) -> str:
    """Rows as ``header: value`` lines, one blank line between records.

    A CSV pasted in verbatim retrieves badly — the header appears once, so every
    row after the first is a list of bare values with nothing saying what they
    mean. Repeating the header per row costs tokens and buys a row that can be
    matched and read on its own.
    """
    reader = csv.reader(io.StringIO(raw))
    try:
        header = next(reader)
    except StopIteration:
        return ""

    lines: list[str] = []
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue
        pairs = [
            f"{(header[i] if i < len(header) else f'column {i + 1}').strip()}: {cell.strip()}"
            for i, cell in enumerate(row)
            if cell.strip()
        ]
        if pairs:
            lines.append("\n".join(pairs))
    return "\n\n".join(lines)


def extract_text(filename: str, data: bytes) -> str:
    """Bytes → plain text, chosen by suffix.

    Suffix rather than sniffing: the suffix is what upload validated and what the
    ``document`` row records, so a mismatch between the two decisions is
    impossible by construction.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedDocument(
            f"no text extractor for {suffix!r} — supported: {sorted(SUPPORTED_SUFFIXES)}"
        )

    raw = _decode(data)
    if suffix == ".html":
        parser = _TextExtractor()
        parser.feed(raw)
        parser.close()
        raw = parser.text()
    elif suffix == ".csv":
        raw = _csv_to_text(raw)

    # Collapse runs of blank lines so paragraph splitting has one meaning.
    return re.sub(r"\n{3,}", "\n\n", raw).strip()


# ── chunking ──────────────────────────────────────────────────────────────


def _encoder():
    import tiktoken

    return tiktoken.get_encoding(_ENCODING)


def _count(encoder, text: str) -> int:
    return len(encoder.encode(text))


def _sections(text: str) -> list[tuple[str | None, str]]:
    """Split on markdown headings into ``(heading, body)``.

    A document with no headings is one section with ``None`` — the caller does
    not branch on whether the format was markdown.
    """
    sections: list[tuple[str | None, str]] = []
    heading: str | None = None
    body: list[str] = []

    for line in text.split("\n"):
        match = _ATX_HEADING.match(line)
        if match:
            if any(part.strip() for part in body):
                sections.append((heading, "\n".join(body).strip()))
            heading = match.group(2).strip()
            body = []
        else:
            body.append(line)

    if any(part.strip() for part in body):
        sections.append((heading, "\n".join(body).strip()))
    return sections or ([(None, text)] if text.strip() else [])


def _paragraphs(body: str) -> list[str]:
    return [p.strip() for p in body.split("\n\n") if p.strip()]


def _split_oversized(encoder, paragraph: str) -> list[str]:
    """A single paragraph larger than the target, cut on token count.

    Sentence boundaries first, because cutting mid-sentence produces a chunk
    that ends in the middle of a clause and embeds as something neither half
    means. A sentence that is *still* too long — a table row, a minified line —
    is cut on tokens, which is ugly but bounded; the alternative is a chunk the
    embedding API rejects outright.
    """
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    out: list[str] = []
    current: list[str] = []

    for sentence in sentences:
        candidate = " ".join([*current, sentence]).strip()
        if current and _count(encoder, candidate) > TARGET_TOKENS:
            out.append(" ".join(current).strip())
            current = [sentence]
        else:
            current = [*current, sentence]

    if current:
        out.append(" ".join(current).strip())

    final: list[str] = []
    for piece in out:
        if _count(encoder, piece) <= TARGET_TOKENS:
            final.append(piece)
            continue
        tokens = encoder.encode(piece)
        for start in range(0, len(tokens), TARGET_TOKENS):
            final.append(encoder.decode(tokens[start : start + TARGET_TOKENS]).strip())
    return [piece for piece in final if piece]


def _overlap_prefix(encoder, text: str) -> str:
    """The tail of a chunk, to open the next one."""
    tokens = encoder.encode(text)
    if len(tokens) <= OVERLAP_TOKENS:
        return text
    return encoder.decode(tokens[-OVERLAP_TOKENS:]).strip()


def chunk_document(filename: str, data: bytes) -> list[Chunk]:
    """The whole path: bytes → text → chunks, ordered within the document.

    ``ordinal`` is position within *this* document, which is what the lexical and
    graph retrievers use to order passages from one file. It is not part of the
    chunk's identity — see the module docstring.
    """
    text = extract_text(filename, data)
    if not text:
        return []

    encoder = _encoder()
    pieces: list[tuple[str | None, str]] = []

    for heading, body in _sections(text):
        current = ""
        for paragraph in _paragraphs(body):
            if _count(encoder, paragraph) > TARGET_TOKENS:
                if current:
                    pieces.append((heading, current))
                    current = ""
                pieces.extend((heading, piece) for piece in _split_oversized(encoder, paragraph))
                continue

            candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
            if current and _count(encoder, candidate) > TARGET_TOKENS:
                pieces.append((heading, current))
                # Open the next chunk with the tail of this one so a statement
                # split across the boundary survives whole somewhere.
                current = f"{_overlap_prefix(encoder, current)}\n\n{paragraph}".strip()
            else:
                current = candidate

        if current:
            pieces.append((heading, current))

    # Merge a runt tail backwards. Done after assembly rather than during, so a
    # section that is legitimately short stays its own chunk and only a trailing
    # fragment of a longer one is absorbed.
    merged: list[tuple[str | None, str]] = []
    for heading, body in pieces:
        if (
            merged
            and merged[-1][0] == heading
            and _count(encoder, body) < MIN_TOKENS
        ):
            merged[-1] = (heading, f"{merged[-1][1]}\n\n{body}")
        else:
            merged.append((heading, body))

    chunks: list[Chunk] = []
    seen: set[str] = set()
    for heading, body in merged:
        # A heading breadcrumb, so a chunk lifted out of the middle of a manual
        # still says which section it came from.
        body_text = f"{heading}\n\n{body}" if heading else body
        identity = canonical_id(body_text)
        if identity in seen:
            # Genuinely duplicated content within one document — a repeated
            # boilerplate block. Two chunks with one id would collide on the
            # (collection, canonical_id, build) unique index and the second
            # would be silently dropped by ON CONFLICT, so drop it here where it
            # can be counted instead.
            logger.debug("dropping duplicate chunk %s in %s", identity, filename)
            continue
        seen.add(identity)
        chunks.append(
            Chunk(canonical_id=identity, ordinal=len(chunks), text=body_text, heading=heading)
        )
    return chunks
