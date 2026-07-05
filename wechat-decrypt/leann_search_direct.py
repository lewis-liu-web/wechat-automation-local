#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Direct vector search over a LEANN index without using the leann CLI.

This script is meant to be executed by the LEANN-core Python interpreter
(3.13, with sentence_transformers + faiss available).  It rebuilds/uses a
plain FAISS index stored next to the LEANN metadata so that search works on
Windows, where the official ``leann search`` CLI currently crashes (ctypes
libc flush + ZMQ/faiss recompute issues).

Inputs (argv):
    1. index_dir   -- directory containing documents.leann.meta.json
    2. query       -- search query
    3. top_k       -- number of results (default 5)

Output:
    JSON list of hits on stdout.  Each hit has id, score, text, metadata.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np


def _find_leann_backend_hnsw():
    """Return the leann_backend_hnsw package root from the active env."""
    for sp in sys.path:
        candidate = Path(sp) / "leann_backend_hnsw"
        if candidate.is_dir() and (candidate / "faiss.py").exists():
            return str(candidate)
    # Fallback: sibling of leann package
    try:
        import leann
        return str(Path(leann.__file__).resolve().parent.parent / "leann_backend_hnsw")
    except Exception:
        pass
    return None


def _load_faiss():
    backend_dir = _find_leann_backend_hnsw()
    if backend_dir and backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    from leann_backend_hnsw import faiss  # type: ignore
    return faiss


def _load_model(model_name: str):
    """Load the SentenceTransformer model (uses sentence_transformers cache)."""
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(model_name)


def _read_passages(passages_path: Path):
    passages = []
    with passages_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            passages.append(json.loads(line))
    return passages


def _build_faiss_index(index_dir: Path, passages: list, meta: dict, faiss, model):
    """Build a plain FAISS HNSW index from the LEANN passages file."""
    texts = [p["text"] for p in passages]
    embs = model.encode(
        texts,
        batch_size=16,
        convert_to_numpy=True,
        normalize_embeddings=False,
        show_progress_bar=False,
    )
    embs = np.asarray(embs, dtype=np.float32)

    dim = embs.shape[1]
    # Use the same graph degree / complexity as the original LEANN metadata.
    backend_kwargs = meta.get("backend_kwargs", {})
    graph_degree = int(backend_kwargs.get("graph_degree", 32))
    complexity = int(backend_kwargs.get("complexity", 64))

    idx = faiss.IndexHNSWFlat(dim, graph_degree)
    idx.hnsw.efConstruction = complexity
    idx.add(embs.shape[0], faiss.swig_ptr(embs))

    faiss_index_path = index_dir / "documents.faiss.index"
    # The custom LEANN faiss build has a non-standard write API; fall back to
    # the standard FAISS write method when available.
    if hasattr(faiss, "write_index"):
        faiss.write_index(idx, str(faiss_index_path))
    else:
        raise RuntimeError("faiss.write_index is not available")
    return idx


def _load_or_build_index(index_dir: Path, meta: dict, faiss, model=None):
    faiss_index_path = index_dir / "documents.faiss.index"
    passages_path = index_dir / "documents.leann.passages.jsonl"
    meta_path = index_dir / "documents.leann.meta.json"

    rebuild = False
    if not faiss_index_path.exists():
        rebuild = True
    else:
        # Rebuild if passages or metadata are newer than the faiss index.
        idx_mtime = faiss_index_path.stat().st_mtime
        if passages_path.exists() and passages_path.stat().st_mtime > idx_mtime:
            rebuild = True
        if meta_path.exists() and meta_path.stat().st_mtime > idx_mtime:
            rebuild = True

    passages = _read_passages(passages_path)
    if rebuild:
        if model is None:
            model = _load_model(meta.get("embedding_model") or "facebook/contriever")
        idx = _build_faiss_index(index_dir, passages, meta, faiss, model)
    else:
        idx = faiss.read_index(str(faiss_index_path))
    return idx, passages


def _search(index_dir: Path, query: str, top_k: int):
    faiss = _load_faiss()
    meta_path = index_dir / "documents.leann.meta.json"
    with meta_path.open(encoding="utf-8") as f:
        meta = json.load(f)

    model_name = meta.get("embedding_model") or "facebook/contriever"
    model = _load_model(model_name)
    idx, passages = _load_or_build_index(index_dir, meta, faiss, model=model)

    qv = model.encode(
        [query],
        convert_to_numpy=True,
        normalize_embeddings=False,
        show_progress_bar=False,
    )
    qv = np.asarray(qv, dtype=np.float32)

    distances = np.empty((1, top_k), dtype=np.float32)
    labels = np.empty((1, top_k), dtype=np.int64)
    idx.search(1, faiss.swig_ptr(qv), top_k, faiss.swig_ptr(distances), faiss.swig_ptr(labels))

    results = []
    for label, dist in zip(labels[0], distances[0]):
        label = int(label)
        if 0 <= label < len(passages):
            p = passages[label]
            results.append({
                "id": str(p.get("id", label)),
                "score": float(dist),
                "text": str(p.get("text", "")),
                "metadata": p.get("metadata", {}),
            })
    return results


def main():
    parser = argparse.ArgumentParser(description="Direct FAISS search over a LEANN index")
    parser.add_argument("index_dir")
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    t0 = time.time()
    try:
        results = _search(Path(args.index_dir), args.query, args.top_k)
        payload = {
            "ok": True,
            "hits": results,
            "count": len(results),
            "elapsed": time.time() - t0,
        }
    except Exception as exc:
        payload = {
            "ok": False,
            "hits": [],
            "count": 0,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "elapsed": time.time() - t0,
        }
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
