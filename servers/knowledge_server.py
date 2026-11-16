"""
Knowledge MCP server: a small searchable library of markdown documents.

The researcher agent searches here and cites what it finds as [doc:<id>].
The citation check in the validator then makes sure every cited id is real.

Search is simple keyword matching: a document scores higher when it contains
more of the query words, and rare words count more than common ones.

Run it on its own with:  python servers/knowledge_server.py
"""

import math
import os
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

server = FastMCP("knowledge", log_level="WARNING")

FOLDER = os.environ.get("KNOWLEDGE_DIR", str(Path(__file__).parent.parent / "knowledge"))

# Words too common to be useful in a search.
STOP_WORDS = set("a an and are as at be by for from has in is it of on or that the this to with".split())

documents = {}  # id -> {"id", "title", "text"}


def words_in(text):
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [word for word in words if word not in STOP_WORDS]


def load_documents():
    for path in sorted(Path(FOLDER).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        first_line = text.splitlines()[0] if text else path.stem
        documents[path.stem] = {"id": path.stem, "title": first_line.lstrip("# "), "text": text}


@server.tool()
def search(query: str, limit: int = 3) -> dict:
    """Find the documents that best match the query."""
    query_words = set(words_in(query))

    # Split each document once so the scoring loop can work with words directly.
    document_words = {}
    for document in documents.values():
        document_words[document["id"]] = words_in(document["text"])

    document_counts = {}
    for word in query_words:
        document_counts[word] = 0
        for words in document_words.values():
            if word in words:
                document_counts[word] += 1

    scores = {}
    for document_id, words in document_words.items():
        score = 0.0
        for word in query_words:
            count = words.count(word)
            if count:
                # A match on a rare word matters more than a match on a common word.
                rarity = math.log(1 + len(documents) / document_counts[word])
                score += count * rarity
        if score > 0:
            scores[document_id] = score / math.sqrt(len(words))  # Don't just reward long documents.

    best = sorted(scores, key=scores.get, reverse=True)[:limit]
    results = []
    for document_id in best:
        results.append({
            "id": document_id,
            "title": documents[document_id]["title"],
            "score": round(scores[document_id], 3),
        })
    return {"results": results}


@server.tool()
def get_document(doc_id: str) -> dict:
    """Read a whole document by its id."""
    if doc_id not in documents:
        return {"error": f"no document called {doc_id}"}
    return documents[doc_id]


@server.tool()
def list_documents() -> dict:
    """List every document with its title and length."""
    inventory = []
    for document in documents.values():
        inventory.append({
            "id": document["id"],
            "title": document["title"],
            "words": len(document["text"].split()),
        })
    return {"documents": inventory}


load_documents()

if __name__ == "__main__":
    server.run()
