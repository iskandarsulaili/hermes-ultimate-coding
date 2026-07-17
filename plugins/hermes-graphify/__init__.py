"""
hermes-graphify — Knowledge graph for Hermes via Graphify.

Structural code understanding: query dependency graphs, trace call chains,
find subsystems, and explain concepts. Complements LSP (per-file depth) and
Semble (semantic search) with structural relationships.

DESIGN: Three tools, one workflow:
  1. Semble → find the right file/concept semantically
  2. LSP → verify correctness after every edit
  3. Graphify → explain how it connects to everything else

AUTO FEATURES (v1.1.0):
  - Auto-builds graph.json on session start (no manual command needed)
  - Auto-updates graph when source files change (after a 5s debounce)
  - Auto-injects structural context (god nodes + stats) on first available turn
  - Falls back to JIT build if graph still missing when a graphify tool is called

Requires `graphifyy` package installed (pip install graphifyy).

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
import time
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

# Install dep BEFORE the module-level import attempt — otherwise the
# try/except ImportError below runs first and _GRAPHIFY_AVAILABLE stays
# False for the entire session.
ensure_deps("hermes-graphify", _GRAPHIFY_DEPS, ask=True)

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

# ---------------------------------------------------------------------------
# Auto-build state (session lifecycle tracking)
# ---------------------------------------------------------------------------
_auto_build_started: set[str] = set()  # set of project dirs that have been checked
_auto_build_lock = threading.Lock()

# Debounced auto-update after file writes
_update_debounce_timers: dict[str, threading.Timer] = {}  # cwd -> Timer
_update_debounce_lock = threading.Lock()
_UPDATE_DEBOUNCE_S = 5.0  # seconds to wait after last write before updating

# File extensions to track for staleness checking
_SOURCE_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
    ".swift", ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".scala",
    ".md", ".mdx", ".rst", ".yaml", ".yml", ".json", ".toml", ".sql",
    ".css", ".scss", ".less", ".html", ".xml", ".svg",
})

# Directories to skip during staleness walk
_SKIP_DIRS = frozenset({
    ".git", ".svn", "__pycache__", "node_modules", "venv", ".venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".eggs", "*.egg-info", ".hermes",
    "graphify-out", ".graphify",
})


# =============================================================================
# Configuration from environment (no hardcoded settings)
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
# Background build tracker — JIT graph building with user-facing options
# =============================================================================

# Stores state for on-demand background graph builds.
# Key: resolved graph.json path. Value: {status, process, project_dir, error}
_background_builds: dict = {}
_bg_build_lock = threading.RLock()  # RLock so _prune_old_builds can acquire nested


def _start_background_build(graph_path: str, project_dir: str, *, update: bool = False) -> None:
    """Start graphify extract/update in a daemon thread.

    Idempotent: if a build for *graph_path* is already RUNNING, this is
    a no-op.  A completed ("done") build does NOT block — subsequent
    auto-updates must be able to re-build.

    Uses a captured entry dict so the worker thread modifies its own
    build's metadata, not a newer build's metadata that replaced it
    under the same key.
    """
    with _bg_build_lock:
        # Only block if a build is actively running (not if done)
        if graph_path in _background_builds:
            existing = _background_builds[graph_path]
            if existing["status"] == "running":
                return
            # "done" or "failed" — replace with a fresh entry
            _background_builds.pop(graph_path, None)

        # Evict old completed entries (keep max 20)
        if len(_background_builds) > 20:
            _prune_old_builds()

        # Build an entry dict and CAPTURE IT in the closure below.
        # The worker must use this captured reference, not a fresh
        # _background_builds[graph_path] lookup, to avoid racing
        # with a newer build that replaces this entry.
        entry: dict = {
            "status": "running",
            "project_dir": project_dir,
            "process": None,
            "error": None,
            "update": update,
        }
        _background_builds[graph_path] = entry

    def _build_worker():
        import subprocess
        import time
        mode = "update" if update else "extract"
        logger.info("Background graph %s started for %s", mode, project_dir)
        try:
            if update:
                cmd = ["graphify", "update", project_dir]
            else:
                cmd = ["graphify", "extract", project_dir, "--code-only"]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            # Store process reference for cancellation (use captured entry)
            with _bg_build_lock:
                if entry["status"] != "running":
                    proc.kill()
                    return
                entry["process"] = proc

            stdout, stderr = proc.communicate(timeout=120)

            with _bg_build_lock:
                if entry["status"] != "running":
                    return  # cancelled
                if proc.returncode == 0:
                    if Path(graph_path).exists():
                        _node_count = _quick_node_count(graph_path)
                        if _node_count is not None and _node_count < 5:
                            entry["status"] = "failed"
                            entry["_finished_at"] = time.time()
                            entry["error"] = (
                                f"Build produced only {_node_count} nodes — "
                                f"likely missing tree-sitter language parsers. "
                                f"Install with: pip install tree-sitter-<language>"
                            )
                            logger.warning(
                                "JIT graph produced only %d nodes for %s — "
                                "missing tree-sitter parsers?",
                                _node_count, project_dir,
                            )
                            return
                        if _node_count is None:
                            entry["status"] = "failed"
                            entry["_finished_at"] = time.time()
                            entry["error"] = "Build produced unparseable graph.json"
                            logger.warning(
                                "JIT graph is unparseable for %s — "
                                "_quick_node_count returned None",
                                project_dir,
                            )
                            return
                        entry["status"] = "done"
                        entry["_finished_at"] = time.time()
                        logger.info("Background graph build succeeded for %s", project_dir)
                        return
                    else:
                        entry["status"] = "failed"
                        entry["_finished_at"] = time.time()
                        entry["error"] = "Build succeeded but graph.json still missing"
                        return
                entry["status"] = "failed"
                entry["_finished_at"] = time.time()
                entry["error"] = f"Exit {proc.returncode}: {stderr.strip()[:500]}"
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            with _bg_build_lock:
                if entry["status"] == "running":
                    entry["status"] = "failed"
                    entry["_finished_at"] = time.time()
                    entry["error"] = "Build timed out after 120s"
        except Exception as e:
            with _bg_build_lock:
                if entry["status"] == "running":
                    entry["status"] = "failed"
                    entry["_finished_at"] = time.time()
                    entry["error"] = str(e)

    t = threading.Thread(target=_build_worker, daemon=True)
    t.start()


def _check_background_build(graph_path: str) -> Optional[str]:
    """Check if a background build has completed.  Returns:
    - None if no build was ever started (graph should exist or never needed)
    - 'running' if still building
    - 'done' if finished successfully
    - 'failed' if build failed (error in _background_builds[graph_path]['error'])
    """
    with _bg_build_lock:
        info = _background_builds.get(graph_path)
        if info is None:
            return None
        return info.get("status")


def _prune_old_builds() -> None:
    """Remove completed/failed/cancelled entries when the dict exceeds 20.

    Caller MUST NOT hold _bg_build_lock (this function acquires it).
    """
    with _bg_build_lock:
        # Keep the 5 most recent terminal entries, remove everything older
        terminal_entries = [
            (path, info) for path, info in _background_builds.items()
            if info.get("status") in ("done", "failed", "cancelled")
        ]
        terminal_entries.sort(key=lambda x: x[1].get("_finished_at", 0), reverse=True)
        for path, _ in terminal_entries[5:]:
            _background_builds.pop(path, None)


def _cancel_background_build(graph_path: str) -> None:
    """Cancel a running background build."""
    with _bg_build_lock:
        info = _background_builds.get(graph_path)
        if info is None:
            return
        proc = info.get("process")
        if proc and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        info["status"] = "cancelled"


# =============================================================================
# Auto-build lifecycle hooks
# =============================================================================


def _source_files_changed_since(project_dir: str, since_mtime: float) -> bool:
    """Quick check: are any source files newer than *since_mtime*?

    Walks up to 4 levels deep, skipping common generated/vendor dirs.
    Returns True at the first changed file found.  Designed for speed:
    stops scanning as soon as one change is detected.
    """
    # Directories to always skip (generated, vendored, version control)
    skip = _SKIP_DIRS

    try:
        for root, dirs, files in os.walk(project_dir, topdown=True, followlinks=False):
            # Compute depth from project_dir
            rel = os.path.relpath(root, project_dir)
            depth = 0 if rel == "." else rel.count(os.sep) + 1

            # Prune skipped dirs AND limit depth
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in skip
            ]
            if depth >= 4:
                dirs.clear()  # don't go deeper

            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext not in _SOURCE_EXTENSIONS:
                    continue
                try:
                    fpath = os.path.join(root, f)
                    if os.path.getmtime(fpath) > since_mtime:
                        return True
                except (OSError, PermissionError):
                    continue
    except (PermissionError, OSError):
        pass
    return False


def _check_and_auto_build(cwd: str | None = None) -> str | None:
    """Called at session start: auto-build if missing, auto-update if stale.

    Returns a status string for logging, or None if no action was needed.
    """
    if cwd is None:
        cwd = os.getcwd()
    graph_path = os.path.join(cwd, "graphify-out", "graph.json")
    graph_path_obj = Path(graph_path)

    if not graph_path_obj.parent.exists():
        return None  # No graphify-out dir at all — nothing to do

    if not graph_path_obj.exists():
        # Auto-build: graph is missing
        logger.info("Auto-build: no graph.json found at %s — starting background build", graph_path)
        _start_background_build(graph_path, cwd, update=False)
        return "auto-build started"

    # Graph exists — check staleness
    graph_mtime = graph_path_obj.stat().st_mtime
    if _source_files_changed_since(cwd, graph_mtime):
        logger.info("Auto-update: source files changed since last graph build — starting incremental update")
        _start_background_build(graph_path, cwd, update=True)
        return "auto-update started"

    logger.debug("Graph is up-to-date")
    return None


def _on_session_start(session_id: str = "", platform: str = "", **kwargs) -> None:
    """Hook: auto-build graph on session start.

    Per-directory: tracks which project dirs have been checked so
    starting a new session (/new) in a different project triggers
    a new build automatically.
    """
    if not _GRAPHIFY_AVAILABLE:
        return

    # In gateway mode there's no meaningful project directory — skip.
    if platform and platform not in ("cli", "tui", ""):
        return

    cwd = os.getcwd()
    with _auto_build_lock:
        if cwd in _auto_build_started:
            return
        _auto_build_started.add(cwd)

    try:
        _check_and_auto_build(cwd)
    except Exception as exc:
        logger.warning("Auto-build on session start failed: %s", exc)


def _on_session_reset(session_id: str = "", platform: str = "", **kwargs) -> None:
    """Hook: re-check auto-build on session reset (/new, /clear).

    Resets the per-directory guard so the new session can re-evaluate
    whether a build is needed in the current project dir.
    """
    if not _GRAPHIFY_AVAILABLE:
        return
    # In gateway mode there's no meaningful project directory — skip.
    if platform and platform not in ("cli", "tui", ""):
        return
    with _auto_build_lock:
        # Only clear the CURRENT dir so other directories keep their state
        _auto_build_started.discard(os.getcwd())


def _on_pre_llm_call(
    user_message: str = "",
    is_first_turn: bool = False,
    **kwargs,
) -> dict | None:
    """Hook: inject graph context (god nodes + stats) before LLM calls.

    Injects on the **first turn where the graph is available** — this
    may be turn 2 or 3 if the background build from on_session_start
    hasn't finished yet.  Once injected, the guard skips subsequent
    turns (the agent retains context in conversation history).

    Returns context that gets injected into the user message, preserving
    the system prompt cache.
    """
    if not _GRAPHIFY_AVAILABLE:
        return None
    if not _engine.available():
        return None

    try:
        cwd = os.getcwd()
        graph_path = os.path.join(cwd, "graphify-out", "graph.json")
        graph_file = Path(graph_path)

        # No graph file yet (build still running) — skip this turn.
        # Context will be injected on a later turn when the file exists.
        if not graph_file.exists():
            return None

        # Check if we already injected context for this graph.
        # Uses the graph's mtime as a version stamp so re-builds re-inject.
        graph_key = f"{graph_path}:{graph_file.stat().st_mtime_ns}"
        with _auto_build_lock:
            if graph_key in _auto_build_started:
                return None
            _auto_build_started.add(graph_key)

        G = _engine.get_graph(graph_path)

        # Compact context: stats + top 15 god nodes
        degrees = [(n, G.degree(n)) for n in G.nodes()]
        degrees.sort(key=lambda x: -x[1])

        top_lines = []
        for nid, deg in degrees[:15]:
            d = G.nodes[nid]
            label = d.get("label", nid)
            source = d.get("source_file", "")
            loc = d.get("source_location", "")
            loc_str = f":{loc}" if loc else ""
            filename = os.path.basename(source) if source else "?"
            top_lines.append(f"  {label} ({deg} edges, {filename}{loc_str})")

        # Count communities (cached in graph metadata after first computation)
        community_count = G.graph.get("_community_count")
        if community_count is None:
            communities: set = set()
            for _, data in G.nodes(data=True):
                cid = data.get("community")
                if cid is not None:
                    communities.add(int(cid))
            community_count = len(communities)
            G.graph["_community_count"] = community_count

        node_count = G.number_of_nodes()
        edge_count = G.number_of_edges()

        # Skip context if graph is too small (noise)
        if node_count < 10:
            return None

        context = (
            f"[Project structure]\n"
            f"Code graph: {node_count} symbols, {edge_count} relationships, "
            f"{community_count} subsystems\n"
            f"Most connected symbols:\n"
            + "\n".join(top_lines) +
            "\n"
        )

        return {"context": context}
    except Exception:
        return None


def _on_post_tool_call(tool_name: str = "", args: dict | None = None, **kwargs) -> None:
    """Hook: detect file writes and schedule incremental graph update.

    Debounced per-project-directory: waits _UPDATE_DEBOUNCE_S seconds
    after the last detected write before triggering the update.
    Per-directory timers prevent two concurrent sessions (gateway mode,
    multiple users) from interfering with each other.
    """
    if not _GRAPHIFY_AVAILABLE:
        return

    # Determine whether this tool call likely wrote source files
    if not _tool_is_writing(tool_name, args):
        return

    cwd = os.getcwd()

    # Debounce per directory: cancel any existing timer for this cwd
    with _update_debounce_lock:
        existing = _update_debounce_timers.pop(cwd, None)
        if existing is not None:
            existing.cancel()

        def _do_update():
            """Fire the update, reading cwd from the closure."""
            with _update_debounce_lock:
                _update_debounce_timers.pop(cwd, None)
                current_graph = Path(cwd) / "graphify-out" / "graph.json"
                if not current_graph.exists():
                    return
                logger.info("Detected file changes — auto-updating graph in %s", cwd)
                _start_background_build(str(current_graph), cwd, update=True)

        timer = threading.Timer(_UPDATE_DEBOUNCE_S, _do_update)
        timer.daemon = True
        _update_debounce_timers[cwd] = timer
        timer.start()


def _on_session_end(session_id: str = "", **kwargs) -> None:
    """Hook: clean up debounce timer for current cwd on session end."""
    if not _GRAPHIFY_AVAILABLE:
        return
    cwd = os.getcwd()
    with _update_debounce_lock:
        timer = _update_debounce_timers.pop(cwd, None)
        if timer is not None:
            timer.cancel()


def _tool_is_writing(tool_name: str, args: dict | None) -> bool:
    """Heuristic: did this tool call likely write to project source files?"""
    # Tools that directly write files
    if tool_name in ("write_file", "patch"):
        return True
    # execute_code writes are always code-gen, assume file writes
    if tool_name == "execute_code":
        return True
    # Terminal commands: check for write-like patterns
    if tool_name == "terminal":
        cmd = (args.get("command") or "").strip() if isinstance(args, dict) else ""

        # Redirect-based writes (most common)
        if ">" in cmd:  # has any redirect ( >, >>, 2>, &> )
            return True

        # File modification commands
        file_cmds = [
            " sed", "sed ",  # sed -i (in-place)
            " awk",  # awk -i (in-place)
            "| tee",  # tee redirect
            "git add", "git commit", "git mv", "git rm", "git checkout -b",
            "mv ", "cp ", "rm ", "touch ",
            "npx ", "npm run", "npm init",
            "yarn ", "pnpm ",
        ]
        lower_cmd = cmd.lower()
        for pat in file_cmds:
            if pat in lower_cmd:
                return True

        # Compilers / transformers that produce output files
        build_cmds = ("make", "cmake", "cargo build", "go build", "tsc",
                      "babel", "webpack", "vite build", "next build")
        if any(cmd.startswith(b) for b in build_cmds):
            return True

        return False

    return False


# =============================================================================
# Helper
# =============================================================================


def _quick_node_count(graph_path: str) -> Optional[int]:
    """Quickly read the node count from a graph.json without loading into networkx.
    Returns None if the file can't be read or parsed."""
    try:
        with open(graph_path) as f:
            data = json.load(f)
        # graphify graph.json has a "directed" flag and then node/edge data
        # The exact structure depends on graphifyy version, but typically
        # it has a "nodes" key or is a dict with node-name keys.
        if isinstance(data, dict):
            if "nodes" in data:
                return len(data["nodes"])
            if "directed" in data:
                return len([k for k in data if k != "directed" and k != "links" and k != "multigraph"])
        if isinstance(data, list):
            return len(data)
        return None
    except Exception:
        return None


def _resolve_graph_path(repo: str) -> str:
    """Resolve graph path: if empty, use default env var; if a directory, look for graphify-out/graph.json.

    Uses os.getcwd() at runtime so it follows directory changes mid-session.
    """
    if not repo or repo.strip() == "":
        if _DEFAULT_GRAPH_PATH:
            return _DEFAULT_GRAPH_PATH
        # Use runtime cwd so cd mid-session is respected
        cwd = os.getcwd()
        cwd_graph = os.path.join(cwd, "graphify-out", "graph.json")
        if os.path.exists(cwd_graph):
            return cwd_graph
        return os.path.join(cwd, "graphify-out", "graph.json")

    repo = repo.strip()
    p = Path(repo)
    if p.is_dir():
        return str(p / "graphify-out" / "graph.json")
    return repo


def _check_graph_exists(graph_path: str) -> Optional[str]:
    """Preemptive check: auto-build graph.json on-demand if missing.

    Returns None if graph.json exists (or was built while we waited),
    or a JSON error string explaining what happened.

    BEHAVIOUR:
    1. Graph exists → None (proceed)
    2. Graph missing → start building in BACKGROUND immediately,
       then return a 'building' status with 3 options for the user:
       - wait: call the tool again, it'll check progress
       - background: continue working, build finishes silently
       - cancel: cancel the build
    3. Background build already running → report its status
    """
    p = Path(graph_path)
    if p.exists():
        return None

    # Resolve project root directory
    if p.parent.name == "graphify-out":
        project_dir = str(p.parent.parent)
    else:
        project_dir = str(p.parent)

    # Check if a background build is already running for this path
    bg_status = _check_background_build(graph_path)

    if bg_status == "done":
        if p.exists():
            return None
        # File claim was wrong, clear and rebuild
        with _bg_build_lock:
            _background_builds.pop(graph_path, None)
        # Fall through to start a new build

    if bg_status == "running":
        # Build already in progress — return status for the LLM to relay
        return json.dumps({
            "status": "building",
            "building": True,
            "build_id": graph_path,
            "project_dir": project_dir,
            "message": (
                f"The knowledge graph for {project_dir} is already being built "
                f"in the background. Check back by calling the tool again "
                f"when you think it might be done."
            ),
        })

    if bg_status == "failed":
        err = _background_builds[graph_path].get("error", "Unknown error")
        return json.dumps({
            "success": False,
            "error": f"Previous auto-build failed: {err}",
        })

    if bg_status == "cancelled":
        # User cancelled before — offer to try again
        with _bg_build_lock:
            _background_builds.pop(graph_path, None)

    # ── Start a new background build ─────────────────────────────
    # Starts immediately before the LLM even presents options to the user.
    # Zero time wasted — the build runs while the user decides.
    logger.info("graph.json not found at %s — starting background build", graph_path)
    _start_background_build(graph_path, project_dir)

    # Return building status with 3 options for the LLM to present
    return json.dumps({
        "success": True,
        "status": "building",
        "building": True,
        "build_id": graph_path,
        "project_dir": project_dir,
        "cancel_command": f"graphify_cancel_{graph_path}",
        "message": (
            f"The knowledge graph at {project_dir} has no pre-built graph yet. "
            f"I've started building it automatically in the background "
            f"(graphify extract --code-only — typically 10-60 seconds).  "
            f"Would you like to:\n"
            f"1. Wait for it to finish (default)\n"
            f"2. Continue working and let it finish in background\n"
            f"3. Cancel the build\n"
            f"\n"
            f"Your call will decide — the build is already running either way."
        ),
    })


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
        pre_check = _check_graph_exists(graph_path)
        if pre_check is not None:
            return pre_check
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
        pre_check = _check_graph_exists(graph_path)
        if pre_check is not None:
            return pre_check
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
        pre_check = _check_graph_exists(graph_path)
        if pre_check is not None:
            return pre_check
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
        pre_check = _check_graph_exists(graph_path)
        if pre_check is not None:
            return pre_check
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
        pre_check = _check_graph_exists(graph_path)
        if pre_check is not None:
            return pre_check
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
        pre_check = _check_graph_exists(graph_path)
        if pre_check is not None:
            return pre_check
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
        pre_check = _check_graph_exists(graph_path)
        if pre_check is not None:
            return pre_check
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
    logger.info("Registering hermes-graphify plugin")

    if not _engine.available():
        logger.warning("Graphify not available: %s", _engine.import_error())

    # Register tools
    ctx.register_tool(        name="graphify_query",        toolset="graphify",        schema={
            "name": "graphify_query",
        "description": (
            "CRITICAL: When the user asks HOW something works, HOW things connect, or the architecture — use this. "            "Returns structured dependency relationships (nodes + edges) showing call chains, imports, and subsystems. "            "REPLACES: manually reading 5-20 files to trace connections — this returns the full graph in one call. "            "Saves ~90% of tokens vs read_file for architecture questions. "            "BEST FOR: 'how does auth work?', 'what connects X to Y?', 'show me the module structure'. "            "DO NOT grep/search_files for architecture — use this instead. It auto-builds graph on first use."        ),
        "parameters": {
            "type": "object",            "properties": {                "question": {                    "type": "string",                    "description": "Natural language question or keyword search (e.g. 'how does auth work?', 'database connection flow').",                },                "repo": {                    "type": "string",                    "description": "Project directory containing graphify-out/graph.json. Defaults to HERMES_GRAPHIFY_GRAPH env or cwd.",                },                "depth": {                    "type": "integer",                    "description": "Traversal depth (1-6, default: 3). Higher = more context, more tokens.",                    "default": _DEFAULT_QUERY_DEPTH,                },                "token_budget": {                    "type": "integer",                    "description": "Max output tokens (default: 2000).",                    "default": _DEFAULT_TOKEN_BUDGET,                },            },            "required": ["question"],        },        },
        handler=_handle_graphify_query,    )
    ctx.register_tool(        name="graphify_path",        toolset="graphify",        schema={
            "name": "graphify_path",
        "description": (
            "Trace the connection path between two code concepts — every intermediate node in the dependency chain. "            "CRITICAL when the user asks how two specific things relate. "            "REPLACES: manually reading 10+ files to trace a connection by hand. "            "BEST FOR: 'how does UserService connect to DatabasePool?', 'what's the call chain from handler to DB?'"        ),
        "parameters": {
            "type": "object",            "properties": {                "source": {                    "type": "string",                    "description": "Source concept label or keyword (e.g. 'UserService', 'auth').",                },                "target": {                    "type": "string",                    "description": "Target concept label or keyword (e.g. 'DatabasePool', 'PostgreSQL').",                },                "repo": {                    "type": "string",                    "description": "Project directory containing graphify-out/graph.json.",                },                "max_hops": {                    "type": "integer",                    "description": "Maximum hops to consider (default: 8).",                    "default": 8,                },            },            "required": ["source", "target"],        },        },
        handler=_handle_graphify_path,    )
    ctx.register_tool(        name="graphify_explain",        toolset="graphify",        schema={
            "name": "graphify_explain",
        "description": (
            "Explain a specific code symbol: source file, location, all dependencies, and all dependents. "            "CRITICAL when the user asks 'what does X do?', 'explain Y', or 'who calls/uses Z?'. "            "REPLACES: grepping for a symbol then reading its file manually — returns all relationships in one call. "            "BEST FOR: 'explain RateLimiter', 'what does UserService depend on?', 'who calls this function?'"        ),
        "parameters": {
            "type": "object",            "properties": {                "label": {                    "type": "string",                    "description": "Node label or ID to look up (e.g. 'RateLimiter', 'UserService').",                },                "repo": {                    "type": "string",                    "description": "Project directory containing graphify-out/graph.json.",                },            },            "required": ["label"],        },        },
        handler=_handle_graphify_explain,    )
    ctx.register_tool(        name="graphify_god_nodes",        toolset="graphify",        schema={
            "name": "graphify_god_nodes",
        "description": (
            "Return the most connected nodes (hubs) in the knowledge graph. "            "CRITICAL: first tool to call when onboarding a new codebase — identifies the most important concepts. "            "REPLACES: randomly reading files to understand what matters in a project. "            "BEST FOR: onboarding to a new project, understanding what the core abstractions are."        ),
        "parameters": {
            "type": "object",            "properties": {                "top_n": {                    "type": "integer",                    "description": "Number of top nodes to return (default: 10).",                    "default": 10,                },                "repo": {                    "type": "string",                    "description": "Project directory containing graphify-out/graph.json.",                },            },            "required": [],        },        },
        handler=_handle_graphify_god_nodes,    )
    ctx.register_tool(        name="graphify_stats",        toolset="graphify",        schema={
            "name": "graphify_stats",
        "description": (
            "Return summary statistics for the knowledge graph: node count, edge count, community count, confidence breakdown. "            "Run this first to verify a graph is built and sized appropriately before using other graphify tools."        ),
        "parameters": {
            "type": "object",            "properties": {                "repo": {                    "type": "string",                    "description": "Project directory containing graphify-out/graph.json.",                },            },            "required": [],        },        },
        handler=_handle_graphify_stats,    )
    ctx.register_tool(        name="graphify_find",        toolset="graphify",        schema={
            "name": "graphify_find",
        "description": (
            "Find nodes in the knowledge graph by label or ID — discover the exact label for graphify_explain/graphify_path. "            "In automated coding: use this BEFORE explain/path when you need to find the exact symbol name."        ),
        "parameters": {
            "type": "object",            "properties": {                "label": {                    "type": "string",                    "description": "Node label or ID to search for (e.g. 'RateLimiter', 'UserService').",                },                "repo": {                    "type": "string",                    "description": "Project directory containing graphify-out/graph.json.",                },            },            "required": ["label"],        },        },
        handler=_handle_graphify_find,    )
    ctx.register_tool(        name="graphify_community",        toolset="graphify",        schema={
            "name": "graphify_community",
        "description": (
            "Get all nodes in a community (subsystem). "            "In automated coding: use AFTER graphify_stats to explore a specific subsystem — find all code in a module/feature area."        ),
        "parameters": {
            "type": "object",            "properties": {                "community_id": {                    "type": "integer",                    "description": "Community ID (0-indexed by size, from graphify_stats).",                },                "repo": {                    "type": "string",                    "description": "Project directory containing graphify-out/graph.json.",                },            },            "required": ["community_id"],        },        },
        handler=_handle_graphify_community,    )
    # Register slash command
    ctx.register_command(
        name="graphify",
        toolset="graphify",
        description=(
            "Graphify knowledge graph commands. "
            "Subcommands: status, query <question> [repo], path <source> <target> [repo], "
            "explain <label> [repo], god [repo], stats [repo]"
        ),
        handler=_cmd_graphify,
    )

    # Register lifecycle hooks for auto-build and context injection
    if _GRAPHIFY_AVAILABLE:
        ctx.register_hook("on_session_start", _on_session_start)
        ctx.register_hook("on_session_reset", _on_session_reset)
        ctx.register_hook("on_session_end", _on_session_end)
        ctx.register_hook("pre_llm_call", _on_pre_llm_call)
        ctx.register_hook("post_tool_call", _on_post_tool_call)

    logger.info(
        "hermes-graphify plugin registered: 7 tools, 1 command, "
        "3 hooks (auto-build, context-inject, auto-update)"
    )
    return {"name": "hermes-graphify", "version": "1.0.0"}
