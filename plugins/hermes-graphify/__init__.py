"""
hermes-graphify — Knowledge graph for Hermes via Graphify.

Structural code understanding: query dependency graphs, trace call chains,
find subsystems, and explain concepts. Complements LSP (per-file depth) and
Semble (semantic search) with structural relationships.

DESIGN: Three tools, one workflow:
  1. Semble → find the right file/concept semantically
  2. LSP → verify correctness after every edit
  3. Graphify → explain how it connects to everything else

Requires `graphifyy` package installed (pip install graphifyy) and a
pre-built graph.json (run `graphify extract . && graphify build .` in
your project root).

Survives Hermes updates by living entirely in ~/.hermes/plugins/.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
from array import array
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure _shared package is discoverable (Hermes loads plugins in isolation)
_shared_dir = str(Path(__file__).resolve().parent.parent)
if _shared_dir not in sys.path:
    sys.path.insert(0, _shared_dir)

from _shared.deps import DepSpec, ensure_deps

logger = logging.getLogger("hermes-graphify")

# ---------------------------------------------------------------------------
# JIT dependency management
# ---------------------------------------------------------------------------
_GRAPHIFY_DEPS = [
    DepSpec(
        "networkx",
        ["python3", "-c", "import networkx"],
        install=[sys.executable, "-m", "pip", "install", "graphifyy"],
        purpose="knowledge graph queries (networkx + graphifyy)",
    ),
]

# =============================================================================
# Lazy import of graphify dependencies
# =============================================================================

_GRAPHIFY_AVAILABLE = False
_GRAPHIFY_IMPORT_ERROR: Optional[str] = None

try:
    import networkx as nx
    from networkx.readwrite import json_graph

    _GRAPHIFY_AVAILABLE = True
except ImportError as e:
    _GRAPHIFY_AVAILABLE = False
    _GRAPHIFY_IMPORT_ERROR = f"networkx not installed (pip install graphifyy): {e}"
except Exception as e:
    _GRAPHIFY_AVAILABLE = False
    _GRAPHIFY_IMPORT_ERROR = f"graphify import error: {e}"

# =============================================================================
# Configuration from environment
# =============================================================================


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default


_DEFAULT_GRAPH_PATH = _env_str("HERMES_GRAPHIFY_GRAPH", "")
_CACHE_MAX_SIZE = _env_int("HERMES_GRAPHIFY_CACHE_SIZE", 10)
_DEFAULT_QUERY_DEPTH = _env_int("HERMES_GRAPHIFY_QUERY_DEPTH", 3)
_DEFAULT_TOKEN_BUDGET = _env_int("HERMES_GRAPHIFY_TOKEN_BUDGET", 2000)
_MAX_GRAPH_FILE_SIZE = _env_int("HERMES_GRAPHIFY_MAX_FILE_SIZE", 100 * 1024 * 1024)  # 100MB

# =============================================================================
# Graphify query engine (extracted from graphify/serve.py)
# =============================================================================

# Constants from graphify
_EXACT_MATCH_BONUS = 1000.0
_PREFIX_MATCH_BONUS = 100.0
_SUBSTRING_MATCH_BONUS = 1.0
_SOURCE_MATCH_BONUS = 0.5

_QUERY_STOPWORDS = frozenset({
    "how", "what", "why", "when", "where", "which", "who", "whom", "whose",
    "does", "did", "is", "are", "was", "were", "be", "been", "being",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "has", "have", "had", "the", "and", "but", "not", "for", "from", "with",
    "without", "into", "onto", "off", "that", "this", "these", "those", "there",
    "here", "its", "their", "them", "they", "about", "any", "all", "some",
    "work", "works", "working",
})


def _strip_diacritics(text: str | None) -> str:
    import unicodedata
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _search_tokens(text: str) -> list[str]:
    return re.findall(r"\w+", _strip_diacritics(str(text)).lower())


def _query_terms(question: str) -> list[str]:
    terms: list[str] = []
    for raw in question.split():
        for tok in re.findall(r"\w+", raw.lower()):
            if len(tok) > 2 or not all("a" <= ch <= "z" for ch in tok):
                terms.append(tok)
    content = [t for t in terms if t not in _QUERY_STOPWORDS]
    return content or terms


def _compute_idf(G: "nx.Graph", terms: list[str]) -> dict[str, float]:
    cache: dict[str, float] = G.graph.setdefault("_idf_cache", {})
    N = G.number_of_nodes() or 1
    uncached = [t for t in terms if t not in cache]
    if uncached:
        df: dict[str, int] = {t: 0 for t in uncached}
        for _, data in G.nodes(data=True):
            norm_label = (
                data.get("norm_label") or _strip_diacritics(data.get("label") or "")
            ).lower()
            for t in uncached:
                if t in norm_label:
                    df[t] += 1
        for t in uncached:
            cache[t] = math.log(1 + N / (1 + df[t]))
    return {t: cache.get(t, math.log(1 + N)) for t in terms}


def _trigrams(text: str) -> set[str]:
    if len(text) < 3:
        return {text} if text else set()
    return {text[i:i + 3] for i in range(len(text) - 2)}


def _node_search_text(data: dict, nid: str) -> str:
    norm_label = data.get("norm_label") or _strip_diacritics(data.get("label") or "").lower()
    label_tokens = " ".join(_search_tokens(data.get("label") or ""))
    source = (data.get("source_file") or "").lower()
    source_tokens = " ".join(_search_tokens(data.get("source_file") or ""))
    return "\x00".join((norm_label, label_tokens, str(nid).lower(), source, source_tokens))


def _get_trigram_index(G: "nx.Graph") -> dict:
    idx = G.graph.get("_trigram_index")
    if idx is not None:
        return idx
    ids = list(G.nodes())
    postings: dict[str, array] = {}
    for i, nid in enumerate(ids):
        for g in _trigrams(_node_search_text(G.nodes[nid], nid)):
            bucket = postings.get(g)
            if bucket is None:
                bucket = array("i")
                postings[g] = bucket
            bucket.append(i)
    idx = {"ids": ids, "postings": postings, "set_cache": {}}
    G.graph["_trigram_index"] = idx
    return idx


def _trigram_candidates(G: "nx.Graph", needles: list[str], *, guard_frac: float = 0.10) -> list[str] | None:
    idx = _get_trigram_index(G)
    ids, postings, set_cache = idx["ids"], idx["postings"], idx["set_cache"]
    n = len(ids)
    if n == 0:
        return []
    needles = [s for s in needles if s]
    thresh = int(n * guard_frac)
    for s in needles:
        tgs = _trigrams(s)
        if not tgs or any(len(g) < 3 for g in tgs):
            return None
        present = [len(postings[g]) for g in tgs if g in postings]
        if not present:
            continue
        if min(present) > thresh:
            return None
    cand: set[int] = set()
    for s in needles:
        sets: list[set] | None = []
        for g in _trigrams(s):
            bucket = postings.get(g)
            if bucket is None:
                sets = None
                break
            cached = set_cache.get(g)
            if cached is None:
                cached = set(bucket)
                set_cache[g] = cached
            sets.append(cached)
        if not sets:
            continue
        sets.sort(key=len)
        hit = set(sets[0])
        for other in sets[1:]:
            hit &= other
            if not hit:
                break
        cand |= hit
    return [ids[i] for i in sorted(cand)]


def _score_nodes(G: "nx.Graph", terms: list[str]) -> list[tuple[float, str]]:
    scored = []
    norm_terms = list(dict.fromkeys(tok for t in terms for tok in _search_tokens(t)))
    n_terms = len(norm_terms)
    idf = _compute_idf(G, norm_terms)
    joined = " ".join(norm_terms)
    joined_w = max((idf.get(t, 1.0) for t in norm_terms), default=1.0)
    candidate_ids = _trigram_candidates(G, norm_terms + ([joined] if joined else []))
    node_iter = (
        G.nodes(data=True) if candidate_ids is None
        else ((nid, G.nodes[nid]) for nid in candidate_ids)
    )
    for nid, data in node_iter:
        norm_label = data.get("norm_label") or _strip_diacritics(data.get("label") or "").lower()
        bare_label = norm_label.rstrip("()")
        label_tokens = " ".join(_search_tokens(data.get("label") or ""))
        source = (data.get("source_file") or "").lower()
        score = 0.0
        if joined:
            nid_lower = nid.lower()
            if joined in (norm_label, bare_label, label_tokens, nid_lower):
                score += _EXACT_MATCH_BONUS * 10 * joined_w
            elif (
                norm_label.startswith(joined)
                or bare_label.startswith(joined)
                or label_tokens.startswith(joined)
            ):
                score += _PREFIX_MATCH_BONUS * 10 * joined_w
        matched = 0
        tiered = 0.0
        for t in norm_terms:
            w = idf.get(t, 1.0)
            if t == norm_label or t == bare_label:
                tiered += _EXACT_MATCH_BONUS * w
                matched += 1
            elif norm_label.startswith(t) or bare_label.startswith(t):
                tiered += _PREFIX_MATCH_BONUS * w
                matched += 1
            elif t in norm_label:
                score += _SUBSTRING_MATCH_BONUS * w
                matched += 1
            if t in source:
                score += _SOURCE_MATCH_BONUS * w
        if tiered:
            score += tiered * (matched / n_terms) ** 2
        if score > 0:
            scored.append((score, nid))
    scored.sort(key=lambda s: (-s[0], len(G.nodes[s[1]].get("label") or s[1]), s[1]))
    return scored


def _pick_seeds(
    scored: list[tuple[float, str]],
    max_k: int = 3,
    gap_ratio: float = 0.2,
    *,
    G: "nx.Graph | None" = None,
    terms: list[str] | None = None,
) -> list[str]:
    if not scored:
        return []
    top_score = scored[0][0]
    seeds: list[str] = []
    seen_labels: set[str] = set()
    for score, nid in scored:
        if len(seeds) >= max_k:
            break
        if seeds and score < top_score * gap_ratio:
            break
        if G is not None:
            data = G.nodes[nid]
            key = (data.get("norm_label") or _strip_diacritics(data.get("label") or "").lower()) or nid
        else:
            key = nid
        if key in seen_labels:
            continue
        seen_labels.add(key)
        seeds.append(nid)
    if G is not None and terms:
        norm_terms = sorted({tok for t in terms for tok in _search_tokens(t)})
        for term in norm_terms:
            term_scored = _score_nodes(G, [term])
            if not term_scored:
                continue
            best_score = term_scored[0][0]
            tied = [nid for s, nid in term_scored if s == best_score]
            best_nid = max(tied, key=lambda n: G.degree(n)) if len(tied) > 1 else term_scored[0][1]
            data = G.nodes[best_nid]
            key = (data.get("norm_label") or _strip_diacritics(data.get("label") or "").lower()) or best_nid
            if best_nid not in seeds and key not in seen_labels:
                seen_labels.add(key)
                seeds.append(best_nid)
    return seeds


def _bfs(G: "nx.Graph", start_nodes: list[str], depth: int) -> tuple[set[str], list[tuple]]:
    degrees = [G.degree(n) for n in G.nodes()]
    if degrees:
        degrees_sorted = sorted(degrees)
        p99_idx = int(len(degrees_sorted) * 0.99)
        hub_threshold = max(50, degrees_sorted[p99_idx])
    else:
        hub_threshold = 50
    seed_set = set(start_nodes)
    visited: set[str] = set(start_nodes)
    frontier = set(start_nodes)
    edges_seen: list[tuple] = []
    for _ in range(depth):
        next_frontier: set[str] = set()
        for n in frontier:
            if n not in seed_set and G.degree(n) >= hub_threshold:
                continue
            for neighbor in G.neighbors(n):
                if neighbor not in visited:
                    next_frontier.add(neighbor)
                    edges_seen.append((n, neighbor))
        visited.update(next_frontier)
        frontier = next_frontier
    return visited, edges_seen


def _subgraph_to_text(G: "nx.Graph", nodes: set[str], edges: list[tuple], token_budget: int = 2000, *, seeds: list[str] | None = None) -> str:
    char_budget = token_budget * 3
    lines = []
    seed_set = set(seeds or [])
    ordered = [n for n in (seeds or []) if n in nodes] + \
              sorted(nodes - seed_set, key=lambda n: G.degree(n), reverse=True)
    for nid in ordered:
        d = G.nodes[nid]
        line = (
            f"NODE {d.get('label', nid)} "
            f"[src={d.get('source_file', '')} "
            f"loc={d.get('source_location', '')} "
            f"community={d.get('community_name') or d.get('community', '')}]"
        )
        lines.append(line)
    for u, v in edges:
        if u in nodes and v in nodes:
            raw = G[u][v]
            d = next(iter(raw.values()), {}) if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)) else raw
            context = d.get("context")
            context_suffix = f" context={context}" if context else ""
            line = (
                f"EDGE {G.nodes[u].get('label', u)} "
                f"--{d.get('relation', '')} "
                f"[{d.get('confidence', '')}{context_suffix}]--> "
                f"{G.nodes[v].get('label', v)}"
            )
            lines.append(line)
    output = "\n".join(lines)
    if len(output) > char_budget:
        cut_at = output[:char_budget].rfind("\n")
        cut_at = cut_at if cut_at > 0 else char_budget
        total_nodes = sum(1 for l in lines if l.startswith("NODE "))
        shown_nodes = output[:cut_at].count("\nNODE ") + (1 if output.startswith("NODE ") else 0)
        cut_count = total_nodes - shown_nodes
        output = (
            output[:cut_at]
            + f"\n... (truncated — {cut_count} more nodes cut by ~{token_budget}-token budget."
            f" Narrow with a more specific query or use graphify_explain for a specific symbol)"
        )
    return output


def _query_graph_text(
    G: "nx.Graph",
    question: str,
    *,
    depth: int = 3,
    token_budget: int = 2000,
) -> str:
    terms = _query_terms(question)
    scored = _score_nodes(G, terms)
    start_nodes = _pick_seeds(scored, G=G, terms=terms)
    if not start_nodes:
        return "No matching nodes found."
    nodes, edges = _bfs(G, start_nodes, depth)
    header_parts = [
        f"Traversal: BFS depth={depth}",
        f"Start: {[G.nodes[n].get('label', n) for n in start_nodes]}",
    ]
    header_parts.append(f"{len(nodes)} nodes found")
    header = " | ".join(header_parts) + "\n\n"
    return header + _subgraph_to_text(G, nodes, edges, token_budget, seeds=start_nodes)


def _find_node(G: "nx.Graph", label: str) -> list[str]:
    term = " ".join(_search_tokens(label))
    if not term:
        return []
    norm_query = _strip_diacritics(str(label)).lower().strip()
    source_exact: list[str] = []
    exact: list[str] = []
    prefix: list[str] = []
    substring: list[str] = []
    candidate_ids = _trigram_candidates(G, [term, norm_query])
    node_iter = (
        G.nodes(data=True) if candidate_ids is None
        else ((nid, G.nodes[nid]) for nid in candidate_ids)
    )
    for nid, d in node_iter:
        norm_label = d.get("norm_label") or _strip_diacritics(d.get("label") or "").lower()
        bare_label = norm_label.rstrip("()")
        label_tokens = " ".join(_search_tokens(d.get("label") or ""))
        source_tokens = " ".join(_search_tokens(d.get("source_file") or ""))
        nid_lower = nid.lower()
        if term == source_tokens:
            source_exact.append(nid)
        elif (
            term == norm_label or term == bare_label or term == label_tokens or term == nid_lower
            or norm_query == norm_label or norm_query == bare_label
        ):
            exact.append(nid)
        elif (
            norm_label.startswith(term)
            or bare_label.startswith(term)
            or label_tokens.startswith(term)
            or nid_lower.startswith(term)
            or norm_label.startswith(norm_query)
            or bare_label.startswith(norm_query)
        ):
            prefix.append(nid)
        elif term in norm_label or term in label_tokens or norm_query in norm_label:
            substring.append(nid)
    return source_exact + exact + prefix + substring


def _edge_data(G: "nx.Graph", u: str, v: str) -> dict:
    raw = G[u][v]
    return next(iter(raw.values()), {}) if isinstance(G, (nx.MultiGraph, nx.MultiDiGraph)) else raw


# =============================================================================
# Graph engine — lazy singleton that loads and caches graph.json files
# =============================================================================


class _GraphEngine:
    """Lazy singleton that manages graph.json loading and caching.

    Loads graph.json files on demand, caches them with mtime/size hot-reload
    (same pattern as graphify's MCP server). LRU eviction when cache is full.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._graphs: "OrderedDict[str, nx.Graph]" = OrderedDict()  # LRU-ordered cache
        self._graph_mtimes: Dict[str, tuple[int, int]] = {}  # path -> (mtime_ns, size)

    def _evict_lru(self) -> None:
        """Evict the oldest index if at capacity (caller must hold lock)."""
        while len(self._graphs) >= _CACHE_MAX_SIZE:
            oldest_key, _ = self._graphs.popitem(last=False)
            self._graph_mtimes.pop(oldest_key, None)

    def _touch(self, cache_key: str) -> None:
        """Mark a cache key as recently used (caller must hold lock)."""
        self._graphs.move_to_end(cache_key)

    def _load_graph(self, path: str) -> "nx.Graph":
        """Load a graph.json file and return a NetworkX graph."""
        resolved = Path(path).resolve()
        if resolved.suffix != ".json":
            raise ValueError(f"Graph path must be a .json file, got: {path!r}")
        if not resolved.exists():
            raise FileNotFoundError(f"Graph file not found: {resolved}")
        # Check file size before loading into memory
        file_size = resolved.stat().st_size
        if file_size > _MAX_GRAPH_FILE_SIZE:
            raise ValueError(
                f"Graph file too large: {file_size} bytes exceeds "
                f"HERMES_GRAPHIFY_MAX_FILE_SIZE={_MAX_GRAPH_FILE_SIZE}"
            )
        data = json.loads(resolved.read_text(encoding="utf-8"))
        if "links" not in data and "edges" in data:
            data = dict(data, links=data["edges"])
        data = {**data, "directed": True}
        try:
            G = json_graph.node_link_graph(data, edges="links")
        except TypeError:
            G = json_graph.node_link_graph(data)
        # Warm the trigram index
        _get_trigram_index(G)
        return G

    def get_graph(self, path: str) -> "nx.Graph":
        """Get or load a graph, with mtime/size hot-reload."""
        cache_key = str(Path(path).resolve())

        # Check mtime/size for hot-reload
        try:
            s = Path(cache_key).stat()
            key = (s.st_mtime_ns, s.st_size)
        except FileNotFoundError:
            raise FileNotFoundError(f"graph.json not found: {path}")

        with self._lock:
            cached = self._graphs.get(cache_key)
            cached_key = self._graph_mtimes.get(cache_key)
            if cached is not None and cached_key == key:
                self._touch(cache_key)
                return cached

            # Load or reload
            self._evict_lru()
            G = self._load_graph(cache_key)
            self._graphs[cache_key] = G
            self._graph_mtimes[cache_key] = key
            self._touch(cache_key)
            return G

    def available(self) -> bool:
        return _GRAPHIFY_AVAILABLE

    def import_error(self) -> Optional[str]:
        return _GRAPHIFY_IMPORT_ERROR


_engine = _GraphEngine()


# =============================================================================
# Helper
# =============================================================================


# Capture cwd at import time for stable default repo resolution
_CWD = os.getcwd()


def _resolve_graph_path(repo: str) -> str:
    """Resolve graph path: if empty, use default env var; if a directory, look for graphify-out/graph.json.

    Uses _CWD captured at import time so it's stable across the session.
    """
    if not repo or repo.strip() == "":
        if _DEFAULT_GRAPH_PATH:
            return _DEFAULT_GRAPH_PATH
        # Try cwd (captured at import time)
        cwd_graph = os.path.join(_CWD, "graphify-out", "graph.json")
        if os.path.exists(cwd_graph):
            return cwd_graph
        return os.path.join(_CWD, "graphify-out", "graph.json")

    repo = repo.strip()
    p = Path(repo)
    if p.is_dir():
        return str(p / "graphify-out" / "graph.json")
    return repo


# =============================================================================
# Hermes Tool Handlers
# =============================================================================


def _handle_graphify_query(args: dict, **kwargs: Any) -> str:
    """Handle graphify_query tool call."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    question = args.get("question", "")
    repo = args.get("repo", "")
    depth = max(1, min(args.get("depth", _DEFAULT_QUERY_DEPTH), 6))
    token_budget = max(100, min(args.get("token_budget", _DEFAULT_TOKEN_BUDGET), 10000))

    if not question:
        return json.dumps({"success": False, "error": "question is required"})

    try:
        graph_path = _resolve_graph_path(repo)
        G = _engine.get_graph(graph_path)
        result = _query_graph_text(G, question, depth=depth, token_budget=token_budget)
        return json.dumps({
            "success": True,
            "result": result,
            "question": question,
            "graph_path": graph_path,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _handle_graphify_path(args: dict, **kwargs: Any) -> str:
    """Handle graphify_path tool call — shortest path between two concepts."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    source = args.get("source", "")
    target = args.get("target", "")
    repo = args.get("repo", "")
    max_hops = max(1, min(args.get("max_hops", 8), 20))

    if not source or not target:
        return json.dumps({"success": False, "error": "source and target are required"})

    try:
        graph_path = _resolve_graph_path(repo)
        G = _engine.get_graph(graph_path)

        # Find source and target nodes
        src_scored = _score_nodes(G, [t.lower() for t in source.split()])
        tgt_scored = _score_nodes(G, [t.lower() for t in target.split()])

        if not src_scored:
            return json.dumps({"success": False, "error": f"No node matching source '{source}' found."})
        if not tgt_scored:
            return json.dumps({"success": False, "error": f"No node matching target '{target}' found."})

        src_nid = src_scored[0][1]
        tgt_nid = tgt_scored[0][1]

        if src_nid == tgt_nid:
            return json.dumps({
                "success": True,
                "result": f"'{source}' and '{target}' both resolved to the same node '{src_nid}'.",
                "hops": 0,
            })

        try:
            path_nodes = nx.shortest_path(G.to_undirected(as_view=True), src_nid, tgt_nid)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return json.dumps({
                "success": False,
                "error": f"No path found between '{G.nodes[src_nid].get('label', src_nid)}' and '{G.nodes[tgt_nid].get('label', tgt_nid)}'.",
            })

        hops = len(path_nodes) - 1
        if hops > max_hops:
            return json.dumps({
                "success": False,
                "error": f"Path exceeds max_hops={max_hops} ({hops} hops found).",
            })

        segments = []
        for i in range(len(path_nodes) - 1):
            u, v = path_nodes[i], path_nodes[i + 1]
            if G.has_edge(u, v):
                edata = _edge_data(G, u, v)
                forward = True
            else:
                edata = _edge_data(G, v, u)
                forward = False
            rel = edata.get("relation", "")
            conf = edata.get("confidence", "")
            conf_str = f" [{conf}]" if conf else ""
            if i == 0:
                segments.append(G.nodes[u].get("label", u))
            if forward:
                segments.append(f"--{rel}{conf_str}--> {G.nodes[v].get('label', v)}")
            else:
                segments.append(f"<--{rel}{conf_str}-- {G.nodes[v].get('label', v)}")

        return json.dumps({
            "success": True,
            "result": f"Shortest path ({hops} hops):\n  " + " ".join(segments),
            "hops": hops,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _handle_graphify_explain(args: dict, **kwargs: Any) -> str:
    """Handle graphify_explain tool call — get full details for a specific node."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    label = args.get("label", "")
    repo = args.get("repo", "")

    if not label:
        return json.dumps({"success": False, "error": "label is required"})

    try:
        graph_path = _resolve_graph_path(repo)
        G = _engine.get_graph(graph_path)

        matches = _find_node(G, label)
        if not matches:
            return json.dumps({"success": False, "error": f"No node matching '{label}' found."})

        nid = matches[0]
        d = G.nodes[nid]

        # Build node details
        lines = [
            f"Node: {d.get('label', nid)}",
            f"  ID: {nid}",
            f"  Source: {d.get('source_file', '')} {d.get('source_location', '')}",
            f"  Type: {d.get('file_type', '')}",
            f"  Community: {d.get('community_name') or d.get('community', '')}",
            f"  Degree: {G.degree(nid)}",
            "",
            "Connections:",
        ]

        # Outgoing edges
        for nb in G.successors(nid):
            ed = _edge_data(G, nid, nb)
            rel = ed.get("relation", "")
            conf = ed.get("confidence", "")
            lines.append(f"  --> {G.nodes[nb].get('label', nb)} [{rel}] [{conf}]")

        # Incoming edges
        for nb in G.predecessors(nid):
            ed = _edge_data(G, nb, nid)
            rel = ed.get("relation", "")
            conf = ed.get("confidence", "")
            lines.append(f"  <-- {G.nodes[nb].get('label', nb)} [{rel}] [{conf}]")

        return json.dumps({
            "success": True,
            "result": "\n".join(lines),
            "node_id": nid,
            "degree": G.degree(nid),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _handle_graphify_god_nodes(args: dict, **kwargs: Any) -> str:
    """Handle graphify_god_nodes tool call — most connected nodes."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    top_n = max(1, min(args.get("top_n", 10), 100))
    repo = args.get("repo", "")

    try:
        graph_path = _resolve_graph_path(repo)
        G = _engine.get_graph(graph_path)

        # Compute god nodes by degree
        degrees = [(n, G.degree(n)) for n in G.nodes()]
        degrees.sort(key=lambda x: -x[1])

        nodes = []
        for nid, deg in degrees[:top_n]:
            d = G.nodes[nid]
            nodes.append({
                "label": d.get("label", nid),
                "degree": deg,
                "source_file": d.get("source_file", ""),
                "community": d.get("community_name") or d.get("community", ""),
            })

        return json.dumps({
            "success": True,
            "god_nodes": nodes,
            "total_nodes": G.number_of_nodes(),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _handle_graphify_stats(args: dict, **kwargs: Any) -> str:
    """Handle graphify_stats tool call — graph statistics."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    repo = args.get("repo", "")

    try:
        graph_path = _resolve_graph_path(repo)
        G = _engine.get_graph(graph_path)

        # Count confidence levels
        confs = [d.get("confidence", "EXTRACTED") for _, _, d in G.edges(data=True)]
        total = len(confs) or 1

        # Count communities
        communities: dict = {}
        for _, data in G.nodes(data=True):
            cid = data.get("community")
            if cid is not None:
                communities.setdefault(int(cid), 0)
                communities[int(cid)] += 1

        return json.dumps({
            "success": True,
            "stats": {
                "nodes": G.number_of_nodes(),
                "edges": G.number_of_edges(),
                "communities": len(communities),
                "extracted_pct": round(confs.count("EXTRACTED") / total * 100),
                "inferred_pct": round(confs.count("INFERRED") / total * 100),
                "ambiguous_pct": round(confs.count("AMBIGUOUS") / total * 100),
            },
            "graph_path": graph_path,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _handle_graphify_find(args: dict, **kwargs: Any) -> str:
    """Handle graphify_find tool call — find nodes by label or ID."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    label = args.get("label", "")
    repo = args.get("repo", "")

    if not label:
        return json.dumps({"success": False, "error": "label is required"})

    try:
        graph_path = _resolve_graph_path(repo)
        G = _engine.get_graph(graph_path)

        matches = _find_node(G, label)
        results = []
        for nid in matches:
            d = G.nodes[nid]
            results.append({
                "label": d.get("label", nid),
                "id": nid,
                "source_file": d.get("source_file", ""),
                "source_location": d.get("source_location", ""),
                "community": d.get("community_name") or d.get("community", ""),
                "degree": G.degree(nid),
            })

        return json.dumps({
            "success": True,
            "results": results,
            "count": len(results),
            "query": label,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


def _handle_graphify_community(args: dict, **kwargs: Any) -> str:
    """Handle graphify_community tool call — get all nodes in a community."""
    if not _engine.available():
        return json.dumps({"success": False, "error": _engine.import_error()})

    community_id = args.get("community_id", None)
    repo = args.get("repo", "")

    if community_id is None:
        return json.dumps({"success": False, "error": "community_id is required"})

    try:
        graph_path = _resolve_graph_path(repo)
        G = _engine.get_graph(graph_path)

        # Build community map
        communities: dict = {}
        for nid, data in G.nodes(data=True):
            cid = data.get("community")
            if cid is not None:
                cid = int(cid)
                communities.setdefault(cid, [])
                communities[cid].append(nid)

        cid = int(community_id)
        nodes = communities.get(cid, [])
        if not nodes:
            return json.dumps({"success": False, "error": f"Community {cid} not found."})

        results = []
        for nid in nodes:
            d = G.nodes[nid]
            results.append({
                "label": d.get("label", nid),
                "source_file": d.get("source_file", ""),
                "degree": G.degree(nid),
            })

        return json.dumps({
            "success": True,
            "community_id": cid,
            "community_name": G.nodes[nodes[0]].get("community_name", f"Community {cid}"),
            "node_count": len(results),
            "nodes": results,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Slash command handler
# =============================================================================


def _cmd_graphify(raw_args: str) -> str:
    """Handle the /graphify slash command."""
    parts = raw_args.strip().split(maxsplit=3)
    subcommand = parts[0] if parts else "status"
    arg1 = parts[1] if len(parts) > 1 else ""
    arg2 = parts[2] if len(parts) > 2 else ""
    arg3 = parts[3] if len(parts) > 3 else ""

    if not _engine.available():
        return f"Error: {_engine.import_error()}"

    try:
        if subcommand == "status":
            with _engine._lock:
                cached_count = len(_engine._graphs)
            return (
                f"Graphify: {'✓ available' if _engine.available() else '✗ unavailable'}\n"
                f"Default graph: {_DEFAULT_GRAPH_PATH or 'not set (use repo= param)'}\n"
                f"Cached graphs: {cached_count}"
            )

        elif subcommand == "query":
            if not arg1:
                return "Usage: /graphify query <question> [repo]"
            result = json.loads(_handle_graphify_query({"question": arg1, "repo": arg2}))
            if not result.get("success"):
                return f"Error: {result.get('error')}"
            return result.get("result", "No result")

        elif subcommand == "path":
            if not arg1 or not arg2:
                return "Usage: /graphify path <source> <target> [repo]"
            result = json.loads(_handle_graphify_path({"source": arg1, "target": arg2, "repo": arg3}))
            if not result.get("success"):
                return f"Error: {result.get('error')}"
            return result.get("result", "No result")

        elif subcommand == "explain":
            if not arg1:
                return "Usage: /graphify explain <label> [repo]"
            result = json.loads(_handle_graphify_explain({"label": arg1, "repo": arg2}))
            if not result.get("success"):
                return f"Error: {result.get('error')}"
            return result.get("result", "No result")

        elif subcommand == "god":
            result = json.loads(_handle_graphify_god_nodes({"top_n": 10, "repo": arg1}))
            if not result.get("success"):
                return f"Error: {result.get('error')}"
            nodes = result.get("god_nodes", [])
            lines = ["## God Nodes (most connected)"]
            for i, n in enumerate(nodes, 1):
                lines.append(f"  {i}. {n['label']} — {n['degree']} edges [{n.get('source_file', '')}]")
            return "\n".join(lines)

        elif subcommand == "stats":
            result = json.loads(_handle_graphify_stats({"repo": arg1}))
            if not result.get("success"):
                return f"Error: {result.get('error')}"
            s = result["stats"]
            return (
                f"## Graph Stats\n"
                f"  Nodes: {s['nodes']}\n"
                f"  Edges: {s['edges']}\n"
                f"  Communities: {s['communities']}\n"
                f"  EXTRACTED: {s['extracted_pct']}%\n"
                f"  INFERRED: {s['inferred_pct']}%\n"
                f"  AMBIGUOUS: {s['ambiguous_pct']}%"
            )

        else:
            return (
                "## Graphify Commands\n"
                "  /graphify status — check Graphify status\n"
                "  /graphify query <question> [repo] — search the knowledge graph\n"
                "  /graphify path <source> <target> [repo] — shortest path between concepts\n"
                "  /graphify explain <label> [repo] — full details for a node\n"
                "  /graphify god [repo] — most connected nodes\n"
                "  /graphify stats [repo] — graph statistics"
            )

    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# Hermes Plugin Registration
# =============================================================================


def register(ctx: Any) -> Dict[str, Any]:
    """Register the Hermes plugin — tools and slash commands."""
    ensure_deps("hermes-graphify", _GRAPHIFY_DEPS)
    logger.info("Registering hermes-graphify plugin")

    if not _engine.available():
        logger.warning("Graphify not available: %s", _engine.import_error())

    # Register tools
    ctx.register_tool(
        name="graphify_query",
        description=(
            "Search the knowledge graph using natural language. "
            "Returns relevant nodes and edges as text context — like a dependency graph for your question. "
            "BEST FOR: 'how does auth work?', 'what connects the database to the API?', "
            "'show me the module structure'. "
            "Complements Semble: Semble finds files by concept, Graphify shows how they connect. "
            "Requires a pre-built graph.json (run graphify extract/build in the project)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Natural language question or keyword search (e.g. 'how does auth work?', 'database connection flow').",
                },
                "repo": {
                    "type": "string",
                    "description": "Project directory containing graphify-out/graph.json. Defaults to HERMES_GRAPHIFY_GRAPH env or cwd.",
                },
                "depth": {
                    "type": "integer",
                    "description": "Traversal depth (1-6, default: 3). Higher = more context, more tokens.",
                    "default": _DEFAULT_QUERY_DEPTH,
                },
                "token_budget": {
                    "type": "integer",
                    "description": "Max output tokens (default: 2000).",
                    "default": _DEFAULT_TOKEN_BUDGET,
                },
            },
            "required": ["question"],
        },
        handler=_handle_graphify_query,
    )

    ctx.register_tool(
        name="graphify_path",
        description=(
            "Find the shortest path between two concepts in the knowledge graph. "
            "Shows how they connect through intermediate nodes. "
            "BEST FOR: 'how does UserService connect to DatabasePool?', "
            "'what's the call chain from the API handler to the database?'"
        ),
        parameters={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "Source concept label or keyword (e.g. 'UserService', 'auth').",
                },
                "target": {
                    "type": "string",
                    "description": "Target concept label or keyword (e.g. 'DatabasePool', 'PostgreSQL').",
                },
                "repo": {
                    "type": "string",
                    "description": "Project directory containing graphify-out/graph.json.",
                },
                "max_hops": {
                    "type": "integer",
                    "description": "Maximum hops to consider (default: 8).",
                    "default": 8,
                },
            },
            "required": ["source", "target"],
        },
        handler=_handle_graphify_path,
    )

    ctx.register_tool(
        name="graphify_explain",
        description=(
            "Get full details for a specific node in the knowledge graph: "
            "source file, location, community, degree, and all connections (incoming and outgoing). "
            "BEST FOR: 'explain RateLimiter', 'what does UserService depend on?', "
            "'who calls this function?'"
        ),
        parameters={
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Node label or ID to look up (e.g. 'RateLimiter', 'UserService').",
                },
                "repo": {
                    "type": "string",
                    "description": "Project directory containing graphify-out/graph.json.",
                },
            },
            "required": ["label"],
        },
        handler=_handle_graphify_explain,
    )

    ctx.register_tool(
        name="graphify_god_nodes",
        description=(
            "Return the most connected nodes in the knowledge graph — the core abstractions. "
            "BEST FOR: understanding what the most important concepts in a codebase are, "
            "what everything flows through."
        ),
        parameters={
            "type": "object",
            "properties": {
                "top_n": {
                    "type": "integer",
                    "description": "Number of top nodes to return (default: 10).",
                    "default": 10,
                },
                "repo": {
                    "type": "string",
                    "description": "Project directory containing graphify-out/graph.json.",
                },
            },
            "required": [],
        },
        handler=_handle_graphify_god_nodes,
    )

    ctx.register_tool(
        name="graphify_stats",
        description=(
            "Return summary statistics for the knowledge graph: node count, edge count, "
            "community count, and confidence breakdown (EXTRACTED/INFERRED/AMBIGUOUS). "
            "BEST FOR: verifying the graph covers what you expect."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Project directory containing graphify-out/graph.json.",
                },
            },
            "required": [],
        },
        handler=_handle_graphify_stats,
    )

    ctx.register_tool(
        name="graphify_find",
        description=(
            "Find nodes in the knowledge graph by label or ID. "
            "Returns matching nodes with source file, location, and degree. "
            "BEST FOR: finding a specific symbol in the graph before using graphify_explain or graphify_path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Node label or ID to search for (e.g. 'RateLimiter', 'UserService').",
                },
                "repo": {
                    "type": "string",
                    "description": "Project directory containing graphify-out/graph.json.",
                },
            },
            "required": ["label"],
        },
        handler=_handle_graphify_find,
    )

    ctx.register_tool(
        name="graphify_community",
        description=(
            "Get all nodes in a community (subsystem) by community ID. "
            "BEST FOR: understanding what belongs to a subsystem, "
            "finding all code in a particular module or feature area."
        ),
        parameters={
            "type": "object",
            "properties": {
                "community_id": {
                    "type": "integer",
                    "description": "Community ID (0-indexed by size, from graphify_stats).",
                },
                "repo": {
                    "type": "string",
                    "description": "Project directory containing graphify-out/graph.json.",
                },
            },
            "required": ["community_id"],
        },
        handler=_handle_graphify_community,
    )

    # Register slash command
    ctx.register_command(
        name="graphify",
        description=(
            "Graphify knowledge graph commands. "
            "Subcommands: status, query <question> [repo], path <source> <target> [repo], "
            "explain <label> [repo], god [repo], stats [repo]"
        ),
        handler=_cmd_graphify,
    )

    logger.info("hermes-graphify plugin registered: 7 tools, 1 command")
    return {"name": "hermes-graphify", "version": "1.0.0"}
