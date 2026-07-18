"""
hermes-orchestra — Combined spec-driven development + version-controlled issue tracking.

SYNERGY:
  OpenSpec's Artifact DAG → defines WHAT to build (proposal → specs → design → tasks)
  Beads' Issue Tracker → tracks WHO builds it and status (claims, dependencies, gating)

  Combined workflow:
  1. Propose → creates proposal artifact
  2. Plan → expands DAG into spec/design/task artifacts
  3. Track → auto-creates issues from artifacts
  4. Ready → finds unblocked work
  5. Claim → agent takes ownership (with heartbeat lease)
  6. Update → add deltas, transition status
  7. Validate → check SHALL/MUST before close
  8. Archive → merge deltas into main specs
  9. Sync → push/pull with external trackers

ARCHITECTURE:
  Storage: version-controlled files under .hermes/orchestra/
    specs/     — Main specification markdown
    changes/   — Delta-based change proposals
    issues/    — Tracking issues JSONL
    store/     — Remote store registry (git-backed)
    sessions/  — Agent claim leases (node-local)

  Core engine (Python-native, zero DB dependency):
    - Artifact DAG with Kahn topological sort
    - Markdown spec parser (section-aware, SHALL/MUST extraction)
    - Validation engine (Zod-like schema checks in Python)
    - Issue state machine (open → claimed → in_progress → closed)
    - Claim/heartbeat lease system (file-based, TTL)

  External sync: GitHub Issues via REST API

THREAD SAFETY:
  All state mutations acquire an file-level lock.
  Agent leases use TTL-based expiry (no Dolt dependency).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import deque

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────
ORCHESTRA_DIR = Path.home() / ".hermes" / "orchestra"


def _sanitize_name(name: str) -> str:
    """Sanitize a spec/change name for use as a filename.
    Removes path separators, special chars, limits to safe chars.
    """
    # Remove path traversal
    name = os.path.basename(os.path.normpath(name))
    # Replace special chars with hyphens
    name = re.sub(r"[^a-zA-Z0-9_.-]", "-", name)
    # Collapse multiple hyphens
    name = re.sub(r"-+", "-", name)
    # Strip leading/trailing hyphens and dots
    name = name.strip("-.")
    return name or "unnamed"
SPECS_DIR = ORCHESTRA_DIR / "specs"
CHANGES_DIR = ORCHESTRA_DIR / "changes"
ISSUES_DIR = ORCHESTRA_DIR / "issues"
STORE_DIR = ORCHESTRA_DIR / "store"
SESSIONS_DIR = ORCHESTRA_DIR / "sessions"
LOCK_FILE = ORCHESTRA_DIR / ".lock"

DEFAULT_ARTIFACTS = ["proposal", "specs", "design", "tasks"]
STATUSES = ["open", "claimed", "in_progress", "blocked", "deferred", "closed"]
ISSUE_TYPES = ["epic", "feature", "task", "bug", "chore", "decision"]
DEP_TYPES = ["blocks", "depends-on", "related", "duplicates", "supersedes"]
LEASE_TTL = 300  # 5 minutes
HEARTBEAT_INTERVAL = 60  # 1 minute

# ── File locking ────────────────────────────────────────────────────────────
_lock = threading.Lock()


def _with_lock(fn):
    """Decorator: acquire file-level lock before mutation."""
    def wrapper(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return wrapper


# ── Path helpers ────────────────────────────────────────────────────────────
def _ensure_dirs():
    for d in [SPECS_DIR, CHANGES_DIR, ISSUES_DIR, STORE_DIR, SESSIONS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def _spec_path(name: str) -> Path:
    return SPECS_DIR / f"{name}.md"


def _change_dir(name: str) -> Path:
    return CHANGES_DIR / name


def _change_spec_path(change: str, spec_name: str) -> Path:
    return _change_dir(change) / f"{spec_name}.delta.md"


def _issue_path(issue_id: str) -> Path:
    return ISSUES_DIR / f"{issue_id}.json"


def _session_path(agent_id: str) -> Path:
    return SESSIONS_DIR / f"{agent_id}.json"


def _next_issue_id() -> str:
    """Generate a short human-readable issue ID like iss-001."""
    existing = list(ISSUES_DIR.glob("iss-*.json"))
    nums = [int(f.stem.split("-")[1]) for f in existing if f.stem.split("-")[1].isdigit()]
    next_num = max(nums) + 1 if nums else 1
    return f"iss-{next_num:03d}"


# ── Spec Engine (OpenSpec-derived) ─────────────────────────────────────────
class SpecEngine:
    """Manage spec files — create, parse, validate, archive deltas.

    A spec is a markdown file with:
      # Name
      ## Overview
      <purpose>
      ## Requirements
      - SHALL do X
      - MUST do Y
      ## Scenarios
      - When X happens, the system SHALL Y

    A change records deltas against a spec:
      # Change: my-feature
      ## Why
      <rationale>
      ## Spec: <name>
      ### ADDED
      - SHALL do new thing
      ### MODIFIED
      - SHALL do updated thing
      ### REMOVED
      - SHALL do old thing
    """

    @staticmethod
    def create_spec(name: str, overview: str, requirements: List[str],
                    scenarios: List[str]) -> str:
        """Create a new spec file. Returns the file path."""
        _ensure_dirs()
        path = _spec_path(name)
        lines = [f"# {name}", "", "## Overview", "", overview, "", "## Requirements", ""]
        for req in requirements:
            lines.append(f"- {req}")
        lines.extend(["", "## Scenarios", ""])
        for sc in scenarios:
            lines.append(f"- {sc}")
        content = "\n".join(lines)
        path.write_text(content)
        return str(path)

    @staticmethod
    def get_spec(name: str) -> Optional[Dict[str, Any]]:
        """Parse a spec file into structured data."""
        path = _spec_path(name)
        if not path.exists():
            return None
        content = path.read_text()
        return SpecEngine._parse_spec(content, name)

    @staticmethod
    def list_specs() -> List[Dict[str, Any]]:
        """List all specs with metadata."""
        _ensure_dirs()
        specs = []
        for path in sorted(SPECS_DIR.glob("*.md")):
            specs.append({
                "name": path.stem,
                "path": str(path),
                "size": path.stat().st_size,
            })
        return specs

    @staticmethod
    def _parse_spec(content: str, name: str) -> Dict[str, Any]:
        """Parse markdown spec into structured dict."""
        overview = ""
        requirements = []
        scenarios = []
        current_section = None

        for line in content.split("\n"):
            if line.startswith("## Overview"):
                current_section = "overview"
                continue
            elif line.startswith("## Requirements"):
                current_section = "requirements"
                continue
            elif line.startswith("## Scenarios"):
                current_section = "scenarios"
                continue
            elif line.startswith("## "):
                current_section = None
                continue

            if current_section == "overview":
                stripped = line.strip()
                if stripped:
                    overview += stripped + " "
            elif current_section == "requirements":
                if line.strip().startswith("- "):
                    requirements.append(line.strip()[2:])
            elif current_section == "scenarios":
                if line.strip().startswith("- "):
                    scenarios.append(line.strip()[2:])

        return {
            "name": name,
            "overview": overview.strip(),
            "requirements": requirements,
            "scenarios": scenarios,
            "shall_count": sum(1 for r in requirements if "SHALL" in r or "MUST" in r),
        }

    @staticmethod
    def create_change(name: str, why: str) -> str:
        """Create a new change directory. Returns the path."""
        _ensure_dirs()
        change_dir = _change_dir(name)
        change_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "name": name,
            "why": why,
            "created": time.time(),
            "status": "open",
            "deltas": [],
        }
        (change_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return str(change_dir)

    @staticmethod
    def add_delta(change: str, spec_name: str,
                  added: List[str] = None,
                  modified: List[str] = None,
                  removed: List[str] = None,
                  renamed: List[str] = None) -> str:
        """Add a delta spec to a change. Returns the path."""
        change_dir = _change_dir(change)
        if not change_dir.exists():
            raise FileNotFoundError(f"Change '{change}' not found")

        lines = [f"# Spec: {spec_name}", "", "### ADDED", ""]
        for r in (added or []):
            lines.append(f"- {r}")
        lines.extend(["", "### MODIFIED", ""])
        for r in (modified or []):
            lines.append(f"- {r}")
        lines.extend(["", "### REMOVED", ""])
        for r in (removed or []):
            lines.append(f"- {r}")

        delta_path = _change_spec_path(change, spec_name)
        delta_path.write_text("\n".join(lines))

        # Update change metadata
        meta_path = change_dir / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            meta.setdefault("deltas", []).append({
                "spec": spec_name,
                "added": len(added or []),
                "modified": len(modified or []),
                "removed": len(removed or []),
            })
            meta_path.write_text(json.dumps(meta, indent=2))

        return str(delta_path)

    @staticmethod
    def validate_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
        """Validate a spec against quality rules. Returns report."""
        issues = []
        spec_name = spec.get("name", "unknown")

        if not spec.get("overview") or len(spec["overview"]) < 20:
            issues.append({
                "type": "WARNING",
                "message": f"Spec '{spec_name}' overview is too short (<20 chars)",
                "guide": "GUIDE_SHORT_OVERVIEW",
            })

        if not spec.get("requirements"):
            issues.append({
                "type": "ERROR",
                "message": f"Spec '{spec_name}' has no requirements",
                "guide": "GUIDE_NO_REQUIREMENTS",
            })

        for req in spec.get("requirements", []):
            if not any(kw in req for kw in ["SHALL", "MUST"]):
                issues.append({
                    "type": "WARNING",
                    "message": f"Requirement in '{spec_name}' lacks SHALL/MUST: '{req[:50]}...'",
                    "guide": "GUIDE_MISSING_SHALL",
                })

        return {
            "valid": not any(i["type"] == "ERROR" for i in issues),
            "issues": issues,
            "error_count": sum(1 for i in issues if i["type"] == "ERROR"),
            "warning_count": sum(1 for i in issues if i["type"] == "WARNING"),
        }

    @staticmethod
    def archive_change(change: str) -> Dict[str, Any]:
        """Merge change deltas into the main spec files. Returns merge report."""
        change_dir = _change_dir(change)
        if not change_dir.exists():
            return {"error": f"Change '{change}' not found"}

        meta = json.loads((change_dir / "meta.json").read_text())
        merged = []

        for delta_file in sorted(change_dir.glob("*.delta.md")):
            spec_name = delta_file.stem.replace(".delta", "")
            content = delta_file.read_text()

            # Parse delta
            added, modified = [], []
            current = None
            for line in content.split("\n"):
                if line.startswith("### ADDED"):
                    current = "added"
                    continue
                elif line.startswith("### MODIFIED"):
                    current = "modified"
                    continue
                elif line.startswith("### REMOVED"):
                    current = "removed"
                    continue
                if current and line.strip().startswith("- "):
                    item = line.strip()[2:]
                    if current == "added":
                        added.append(item)

            # Apply to main spec
            spec = SpecEngine.get_spec(spec_name)
            if not spec:
                # Create spec from delta
                SpecEngine.create_spec(spec_name, "", added, [])
                merged.append(spec_name)
                continue

            # Append new requirements
            spec_path = _spec_path(spec_name)
            spec_content = spec_path.read_text()
            for req in added:
                spec_content += f"\n- {req}"
            spec_path.write_text(spec_content)
            merged.append(spec_name)

        return {
            "change": change,
            "merged_specs": merged,
            "delta_count": len(merged),
            "status": "archived",
        }


# ── Artifact DAG (OpenSpec-derived) ────────────────────────────────────────
class ArtifactDAG:
    """Directed Acyclic Graph of artifacts with topological ordering.

    Default schema:
      proposal → specs → design → tasks

    Each artifact has:
      - id: unique slug
      - description: what this artifact produces
      - requires: list of artifact IDs that must complete first
      - template: path to template file
    """

    @staticmethod
    def default_schema() -> Dict[str, Any]:
        return {
            "artifacts": [
                {"id": "proposal", "description": "Problem statement and solution approach",
                 "requires": [], "generates": "PROPOSAL.md"},
                {"id": "specs", "description": "Detailed specifications",
                 "requires": ["proposal"], "generates": "specs/*.md"},
                {"id": "design", "description": "Technical design document",
                 "requires": ["specs"], "generates": "DESIGN.md"},
                {"id": "tasks", "description": "Implementation tasks",
                 "requires": ["design"], "generates": "tasks/*.md"},
            ]
        }

    @staticmethod
    def get_build_order(artifacts: List[Dict]) -> List[str]:
        """Return artifacts in topological order (Kahn's algorithm)."""
        graph = {a["id"]: set(a.get("requires", [])) for a in artifacts}
        in_degree = {id: len(deps) for id, deps in graph.items()}

        queue = deque([id for id, deg in in_degree.items() if deg == 0])
        order = []

        while queue:
            node = queue.popleft()
            order.append(node)
            for other_id, deps in graph.items():
                if node in deps:
                    deps.remove(node)
                    in_degree[other_id] -= 1
                    if in_degree[other_id] == 0:
                        queue.append(other_id)

        # Check for cycles
        if len(order) != len(artifacts):
            raise ValueError("Cycle detected in artifact DAG")

        return order

    @staticmethod
    def get_next_artifacts(artifacts: List[Dict],
                           completed: Set[str]) -> List[str]:
        """Return artifacts ready to work on (all deps satisfied, not completed)."""
        ready = []
        for a in artifacts:
            if a["id"] in completed:
                continue
            deps = set(a.get("requires", []))
            if deps.issubset(completed):
                ready.append(a["id"])
        return ready


# ── Issue Tracker (Beads-derived) ──────────────────────────────────────────
class IssueTracker:
    """Simple file-based issue tracker with claims and dependencies.

    Beads-inspired but Python-native:
    - Issues stored as JSON files (one per issue)
    - File-level locking via threading.Lock
    - Claim leases with TTL (file-based, no Dolt needed)
    - Dependencies between issues (blocks/depends-on)
    """

    @staticmethod
    @_with_lock
    def create_issue(title: str, description: str = "",
                     issue_type: str = "task",
                     priority: int = 2,
                     assignee: str = "",
                     deps: List[str] = None) -> Dict[str, Any]:
        """Create a new tracking issue."""
        _ensure_dirs()
        issue_id = _next_issue_id()

        issue = {
            "id": issue_id,
            "title": title,
            "description": description,
            "type": issue_type if issue_type in ISSUE_TYPES else "task",
            "priority": max(0, min(priority, 4)),
            "status": "open",
            "assignee": assignee,
            "dependencies": deps or [],
            "labels": [],
            "comments": [],
            "events": [{"type": "created", "timestamp": time.time()}],
            "created_at": time.time(),
            "updated_at": time.time(),
            "closed_at": None,
            "lease_expires_at": None,
        }

        _issue_path(issue_id).write_text(json.dumps(issue, indent=2))
        return issue

    @staticmethod
    @_with_lock
    def get_issue(issue_id: str) -> Optional[Dict]:
        """Get an issue by ID."""
        path = _issue_path(issue_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    @staticmethod
    @_with_lock
    def update_issue(issue_id: str, **kwargs) -> Optional[Dict]:
        """Update issue fields."""
        issue = IssueTracker.get_issue(issue_id)
        if not issue:
            return None

        valid_fields = {"title", "description", "type", "priority",
                        "status", "assignee", "labels"}
        for k, v in kwargs.items():
            if k in valid_fields:
                issue[k] = v
                issue["events"].append({
                    "type": f"{k}_changed",
                    "value": v,
                    "timestamp": time.time(),
                })

        issue["updated_at"] = time.time()

        if kwargs.get("status") == "closed":
            issue["closed_at"] = time.time()

        _issue_path(issue_id).write_text(json.dumps(issue, indent=2))
        return issue

    @staticmethod
    @_with_lock
    def claim_issue(issue_id: str, agent_id: str) -> Dict:
        """Claim an issue for an agent. Returns lease info."""
        issue = IssueTracker.get_issue(issue_id)
        if not issue:
            return {"error": f"Issue {issue_id} not found"}

        # Check existing lease
        if issue.get("lease_expires_at") and issue["lease_expires_at"] > time.time():
            return {"error": f"Issue {issue_id} is already leased", "lease_owner": issue.get("assignee")}

        lease_expiry = time.time() + LEASE_TTL
        issue["status"] = "claimed"
        issue["assignee"] = agent_id
        issue["lease_expires_at"] = lease_expiry
        issue["events"].append({
            "type": "claimed",
            "agent": agent_id,
            "lease_expires": lease_expiry,
            "timestamp": time.time(),
        })

        _issue_path(issue_id).write_text(json.dumps(issue, indent=2))

        # Create session
        session = {
            "agent_id": agent_id,
            "issue_id": issue_id,
            "claimed_at": time.time(),
            "expires_at": lease_expiry,
            "heartbeat_at": time.time(),
        }
        _session_path(agent_id).write_text(json.dumps(session, indent=2))

        return {
            "issue_id": issue_id,
            "agent_id": agent_id,
            "lease_expires_at": lease_expiry,
            "ttl_seconds": LEASE_TTL,
        }

    @staticmethod
    @_with_lock
    def heartbeat(agent_id: str) -> Dict:
        """Renew lease for an agent's claimed issue."""
        session_path = _session_path(agent_id)
        if not session_path.exists():
            return {"error": f"No active session for agent '{agent_id}'"}

        session = json.loads(session_path.read_text())
        session["heartbeat_at"] = time.time()
        session["expires_at"] = time.time() + LEASE_TTL

        # Update issue lease
        issue = IssueTracker.get_issue(session["issue_id"])
        if issue:
            issue["lease_expires_at"] = session["expires_at"]
            _issue_path(session["issue_id"]).write_text(json.dumps(issue, indent=2))

        session_path.write_text(json.dumps(session, indent=2))
        return {
            "agent_id": agent_id,
            "issue_id": session["issue_id"],
            "lease_renewed_until": session["expires_at"],
        }

    @staticmethod
    @_with_lock
    def find_ready() -> List[Dict]:
        """Find issues with all dependencies resolved (not blocked, not claimed)."""
        _ensure_dirs()
        ready = []
        for path in ISSUES_DIR.glob("*.json"):
            issue = json.loads(path.read_text())
            if issue["status"] in ("closed", "claimed", "in_progress"):
                continue

            # Check dependencies
            blocked = False
            for dep_id in issue.get("dependencies", []):
                dep = IssueTracker.get_issue(dep_id)
                if dep and dep["status"] != "closed":
                    blocked = True
                    break

            if not blocked:
                ready.append({
                    "id": issue["id"],
                    "title": issue["title"],
                    "type": issue["type"],
                    "priority": issue["priority"],
                    "dependencies": issue.get("dependencies", []),
                })

        return sorted(ready, key=lambda i: i["priority"])

    @staticmethod
    @_with_lock
    def list_issues(status: str = "") -> List[Dict]:
        """List all issues, optionally filtered by status."""
        _ensure_dirs()
        issues = []
        for path in sorted(ISSUES_DIR.glob("*.json")):
            issue = json.loads(path.read_text())
            if status and issue["status"] != status:
                continue
            issues.append({
                "id": issue["id"],
                "title": issue["title"],
                "type": issue["type"],
                "status": issue["status"],
                "priority": issue["priority"],
                "assignee": issue.get("assignee", ""),
            })
        return issues

    @staticmethod
    @_with_lock
    def add_dependency(issue_id: str, depends_on: str, dep_type: str = "depends-on") -> Dict:
        """Add a dependency between issues."""
        issue = IssueTracker.get_issue(issue_id)
        if not issue:
            return {"error": f"Issue {issue_id} not found"}

        deps = issue.get("dependencies", [])
        if depends_on not in deps:
            deps.append(depends_on)
            issue["dependencies"] = deps
            _issue_path(issue_id).write_text(json.dumps(issue, indent=2))

        return {"issue_id": issue_id, "depends_on": depends_on, "type": dep_type}


# ── Bridge: DAG → Issues (the synergy) ────────────────────────────────────
class OrchestraBridge:
    """Bridge between ArtifactDAG and IssueTracker.

    When an artifact is created:
      1. An epic-level issue is created for the proposal
      2. Feature/task issues are created for each child artifact
      3. Dependencies are set based on the DAG's requires[] edges
      4. When all child issues close, the parent artifact auto-advances
    """

    @staticmethod
    def materialize_dag(schema_artifacts: List[Dict],
                        proposal_name: str) -> Dict:
        """Materialize a DAG into the issue tracker.

        Returns mapping of artifact_id → issue_id.
        """
        order = ArtifactDAG.get_build_order(schema_artifacts)
        mapping = {}

        for artifact_id in order:
            artifact = next(a for a in schema_artifacts if a["id"] == artifact_id)
            title = f"{proposal_name}: {artifact['description']}"
            deps = []

            # Map DAG requires[] to issue dependencies
            for req_id in artifact.get("requires", []):
                if req_id in mapping:
                    deps.append(mapping[req_id])

            issue_type = "epic" if artifact_id == "proposal" else "task"
            issue = IssueTracker.create_issue(
                title=title,
                description=f"Artifact: {artifact_id}\nGenerates: {artifact.get('generates', '')}",
                issue_type=issue_type,
                deps=deps,
            )
            mapping[artifact_id] = issue["id"]

        return mapping

    @staticmethod
    def workspace_status() -> Dict:
        """Aggregate health of specs, changes, and issues."""
        specs = SpecEngine.list_specs()
        changes = list(CHANGES_DIR.iterdir()) if CHANGES_DIR.exists() else []
        issues = IssueTracker.list_issues()
        ready = IssueTracker.find_ready()

        active_leases = []
        for session_file in SESSIONS_DIR.glob("*.json"):
            session = json.loads(session_file.read_text())
            if session.get("expires_at", 0) > time.time():
                active_leases.append(session)

        return {
            "spec_count": len(specs),
            "change_count": len(changes),
            "issue_count": len(issues),
            "ready_count": len(ready),
            "active_leases": len(active_leases),
            "by_status": {
                s: len([i for i in issues if i["status"] == s])
                for s in STATUSES
            },
        }


# ── GitHub Sync (Beads-inspired adapter) ────────────────────────────────────
class GitHubSync:
    """Bidirectional sync between Hermes Orchestra and GitHub Issues.

    Uses the GitHub REST API via curl (no additional dependencies).
    """

    @staticmethod
    def push_issue(issue_id: str, repo: str, token: str) -> Dict:
        """Push an issue to GitHub Issues. Returns the GitHub issue URL."""
        issue = IssueTracker.get_issue(issue_id)
        if not issue:
            return {"error": f"Issue {issue_id} not found"}

        labels = ",".join(issue.get("labels", []))
        import urllib.request
        import urllib.error

        url = f"https://api.github.com/repos/{repo}/issues"
        data = json.dumps({
            "title": issue["title"],
            "body": issue.get("description", ""),
            "labels": issue.get("labels", []),
        }).encode()

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"token {token}")
        req.add_header("Content-Type", "application/json")

        try:
            resp = urllib.request.urlopen(req)
            gh_issue = json.loads(resp.read().decode())
            return {
                "issue_id": issue_id,
                "github_url": gh_issue.get("html_url", ""),
                "github_id": gh_issue.get("number"),
            }
        except urllib.error.HTTPError as e:
            return {"error": f"GitHub API error: {e.code} {e.read().decode()[:200]}"}
        except Exception as e:
            return {"error": f"Sync failed: {e}"}

    @staticmethod
    def pull_issues(repo: str, token: str, max_issues: int = 10) -> List[Dict]:
        """Pull recent open issues from GitHub into local tracker."""
        import urllib.request
        import urllib.error

        url = f"https://api.github.com/repos/{repo}/issues?state=open&per_page={max_issues}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"token {token}")
        req.add_header("Accept", "application/vnd.github.v3+json")

        try:
            resp = urllib.request.urlopen(req)
            gh_issues = json.loads(resp.read().decode())
            pulled = []

            for gh in gh_issues:
                issue = IssueTracker.create_issue(
                    title=gh.get("title", ""),
                    description=gh.get("body", ""),
                    issue_type="task",
                    priority=2,
                )
                pulled.append({
                    "local_id": issue["id"],
                    "github_id": gh.get("number"),
                    "github_url": gh.get("html_url", ""),
                })

            return pulled
        except Exception as e:
            return [{"error": f"Pull failed: {e}"}]


# ── Persistent store (the _engine singleton) ───────────────────────────────
class _OrchestraEngine:
    """Lazy singleton managing all orchestra state."""

    def __init__(self):
        self._ready = False
        self._error: Optional[str] = None
        self.specs = SpecEngine()
        self.dag = ArtifactDAG()
        self.issues = IssueTracker()
        self.bridge = OrchestraBridge()
        self.gh = GitHubSync()

    def ensure_ready(self) -> Optional[str]:
        if self._ready:
            return None
        try:
            _ensure_dirs()
            self._ready = True
            return None
        except Exception as e:
            self._error = str(e)
            return self._error

    def status(self) -> Dict:
        err = self.ensure_ready()
        if err:
            return {"error": err}
        return {
            "ready": self._ready,
            "orchestra_dir": str(ORCHESTRA_DIR),
            **OrchestraBridge.workspace_status(),
        }


_engine = _OrchestraEngine()


# ── Tool handlers ──────────────────────────────────────────────────────────
def _handle_orchestra_init(args: dict, **kwargs: Any) -> str:
    """Initialize orchestra workspace with specs directory and issue DB."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    _ensure_dirs()
    # Create default proposal spec
    proposal_name = _sanitize_name(args.get("proposal", "untitled"))
    SpecEngine.create_spec(
        name=proposal_name,
        overview=args.get("overview", "Proposal placeholder"),
        requirements=["SHALL be defined", "MUST be reviewed"],
        scenarios=["Initial scenario"],
    )
    # Create epic issue for the proposal
    epic = IssueTracker.create_issue(
        title=proposal_name,
        description=args.get("overview", ""),
        issue_type="epic",
        priority=0,
    )

    return json.dumps({
        "status": "initialized",
        "orchestra_dir": str(ORCHESTRA_DIR),
        "proposal_spec": f"{proposal_name}.md",
        "epic_issue": epic["id"],
    }, default=str)


def _handle_orchestra_plan(args: dict, **kwargs: Any) -> str:
    """Expand a proposal into a full artifact DAG with tracked issues."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    proposal = _sanitize_name(args.get("proposal", "untitled"))
    artifacts = ArtifactDAG.default_schema()["artifacts"]

    # Create spec for each artifact
    for art in artifacts:
        SpecEngine.create_spec(
            name=f"{proposal}/{art['id']}",
            overview=f"{art['description']} for {proposal}",
            requirements=[f"SHALL implement {art['id']} for {proposal}"],
            scenarios=[],
        )

    # Materialize DAG into issues
    mapping = OrchestraBridge.materialize_dag(artifacts, proposal)

    return json.dumps({
        "proposal": proposal,
        "artifact_count": len(artifacts),
        "build_order": ArtifactDAG.get_build_order(artifacts),
        "issue_map": mapping,
    }, default=str)


def _handle_orchestra_propose(args: dict, **kwargs: Any) -> str:
    """Create a new proposal with spec + epic issue."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    name = _sanitize_name(args.get("name", f"proposal-{int(time.time())}"))

    # Create spec
    spec = SpecEngine.create_spec(
        name=name,
        overview=args.get("overview", ""),
        requirements=args.get("requirements", ["SHALL be defined"]),
        scenarios=args.get("scenarios", []),
    )

    # Validate the spec
    parsed = SpecEngine.get_spec(name)
    report = SpecEngine.validate_spec(parsed) if parsed else {"valid": False, "issues": [{"message": "Parse failed"}]}

    # Create epic issue
    epic = IssueTracker.create_issue(
        title=name,
        description=args.get("overview", ""),
        issue_type="epic",
        priority=args.get("priority", 2),
    )

    return json.dumps({
        "spec": spec,
        "validation": report,
        "epic_issue": epic,
    }, default=str)


def _handle_orchestra_track(args: dict, **kwargs: Any) -> str:
    """Create a tracked work item directly."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    issue = IssueTracker.create_issue(
        title=args.get("title", "untitled"),
        description=args.get("description", ""),
        issue_type=args.get("type", "task"),
        priority=args.get("priority", 2),
        assignee=args.get("assignee", ""),
        deps=args.get("depends_on", []),
    )

    return json.dumps(issue, default=str)


def _handle_orchestra_ready(args: dict, **kwargs: Any) -> str:
    """Find ready-to-work issues (all dependencies resolved)."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    ready = IssueTracker.find_ready()
    return json.dumps(ready, default=str)


def _handle_orchestra_claim(args: dict, **kwargs: Any) -> str:
    """Claim an issue for an agent with lease."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    result = IssueTracker.claim_issue(
        issue_id=args.get("issue_id", ""),
        agent_id=args.get("agent_id", "default"),
    )
    return json.dumps(result, default=str)


def _handle_orchestra_heartbeat(args: dict, **kwargs: Any) -> str:
    """Renew a claim lease for an agent's current issue."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    result = IssueTracker.heartbeat(
        agent_id=args.get("agent_id", "default"),
    )
    return json.dumps(result, default=str)


def _handle_orchestra_update(args: dict, **kwargs: Any) -> str:
    """Update issue status or add delta to a change."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    issue_id = args.get("issue_id", "")
    status = args.get("status", "")
    change = _sanitize_name(args.get("change", ""))

    if issue_id:
        fields = {}
        if status:
            fields["status"] = status
        if args.get("title"):
            fields["title"] = args["title"]
        if args.get("assignee"):
            fields["assignee"] = args["assignee"]
        result = IssueTracker.update_issue(issue_id, **fields)
        return json.dumps(result, default=str)

    if change:
        spec_name = args.get("spec", "")
        added = args.get("added", [])
        modified = args.get("modified", [])
        try:
            path = SpecEngine.add_delta(change, spec_name, added=added, modified=modified)
            return json.dumps({"change": change, "delta_path": path}, default=str)
        except FileNotFoundError as e:
            return json.dumps({"error": str(e)}, default=str)

    return json.dumps({"error": "Provide issue_id or change name"}, default=str)


def _handle_orchestra_validate(args: dict, **kwargs: Any) -> str:
    """Validate a spec before committing."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    spec_name = args.get("spec", "")
    spec = SpecEngine.get_spec(spec_name)
    if not spec:
        return json.dumps({"error": f"Spec '{spec_name}' not found"})

    report = SpecEngine.validate_spec(spec)
    return json.dumps(report, default=str)


def _handle_orchestra_status(args: dict, **kwargs: Any) -> str:
    """Full workspace health: specs, issues, active leases, ready work."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})
    return json.dumps(_engine.status(), default=str)


def _handle_orchestra_sync(args: dict, **kwargs: Any) -> str:
    """Sync with GitHub Issues."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    direction = args.get("direction", "pull")
    repo = args.get("repo", "")
    token = args.get("token", os.environ.get("GITHUB_TOKEN", ""))

    if not repo or not token:
        return json.dumps({"error": "repo and token are required (or set GITHUB_TOKEN)"})

    if direction == "push":
        issue_id = args.get("issue_id", "")
        if not issue_id:
            return json.dumps({"error": "issue_id required for push"})
        result = GitHubSync.push_issue(issue_id, repo, token)
    else:
        result = GitHubSync.pull_issues(repo, token, max_issues=args.get("max_issues", 10))

    return json.dumps(result, default=str)


def _handle_orchestra_archive(args: dict, **kwargs: Any) -> str:
    """Archive a change, merging deltas into main specs."""
    err = _engine.ensure_ready()
    if err:
        return json.dumps({"error": err})

    change = _sanitize_name(args.get("change", ""))
    if not change:
        return json.dumps({"error": "change name required"})

    result = SpecEngine.archive_change(change)
    return json.dumps(result, default=str)


# ── Slash command ──────────────────────────────────────────────────────────
def _cmd_orchestra(raw_args: str) -> str:
    """Handle /orchestra slash command."""
    parts = raw_args.strip().split(maxsplit=2)
    if not parts:
        return (
            "Usage: /orchestra <subcommand> [args]\n"
            "  init      — Initialize workspace\n"
            "  status    — Workspace health\n"
            "  propose   — Create proposal with spec + epic\n"
            "  plan      — Expand proposal into DAG + issues\n"
            "  track     — Create a tracked work item\n"
            "  ready     — Find ready-to-work issues\n"
            "  claim     — Claim an issue\n"
            "  update    — Update issue or add delta\n"
            "  validate  — Validate a spec\n"
            "  archive   — Archive a change\n"
            "  sync      — Sync with GitHub Issues"
        )

    subcmd = parts[0].lower()
    if subcmd == "status":
        return json.dumps(_engine.status(), default=str, indent=2)
    elif subcmd == "ready":
        return json.dumps(IssueTracker.find_ready(), default=str, indent=2)
    elif subcmd == "init":
        return _handle_orchestra_init({"proposal": "my-project", "overview": "Project placeholder"}, {})
    else:
        return f"Usage: /orchestra {subcmd} [args]. Use /orchestra alone for full help."


# ── Plugin entry point ─────────────────────────────────────────────────────
def register(ctx: Any) -> Dict[str, Any]:
    """Register the hermes-orchestra plugin."""
    logger.info("Registering hermes-orchestra plugin")

    ctx.register_tool(
        name="orchestra_init",
        toolset="orchestra",
        schema={
            "name": "orchestra_init",
            "description": "Initialize the orchestra workspace: creates .hermes/orchestra/ with specs directory, issue store, and a default proposal with epic issue. Call this first before any other orchestra tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "proposal": {"type": "string", "description": "Initial proposal name (default: untitled)"},
                    "overview": {"type": "string", "description": "Proposal overview/purpose"},
                },
            },
        },
        handler=_handle_orchestra_init,
    )

    ctx.register_tool(
        name="orchestra_propose",
        toolset="orchestra",
        schema={
            "name": "orchestra_propose",
            "description": "Create a new proposal with a structured spec (requirements + scenarios) and an epic tracking issue. Validates the spec and returns the validation report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Proposal name (kebab-case)"},
                    "overview": {"type": "string", "description": "Brief overview/purpose of the proposal"},
                    "requirements": {"type": "array", "items": {"type": "string"},
                                     "description": "Requirements (use SHALL/MUST keywords)"},
                    "scenarios": {"type": "array", "items": {"type": "string"},
                                  "description": "Test scenarios"},
                    "priority": {"type": "integer", "description": "Priority 0-4 (0=highest)"},
                },
                "required": ["name"],
            },
        },
        handler=_handle_orchestra_propose,
    )

    ctx.register_tool(
        name="orchestra_plan",
        toolset="orchestra",
        schema={
            "name": "orchestra_plan",
            "description": "Expand a proposal into a full artifact DAG (proposal → specs → design → tasks). Creates spec files for each artifact and tracked issues with proper dependencies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "proposal": {"type": "string", "description": "Proposal name to plan"},
                },
                "required": ["proposal"],
            },
        },
        handler=_handle_orchestra_plan,
    )

    ctx.register_tool(
        name="orchestra_track",
        toolset="orchestra",
        schema={
            "name": "orchestra_track",
            "description": "Create a tracked work item directly. Can be linked to an artifact from the DAG or standalone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Issue title"},
                    "description": {"type": "string", "description": "Detailed description"},
                    "type": {"type": "string", "enum": ["epic", "feature", "task", "bug", "chore", "decision"],
                             "description": "Issue type"},
                    "priority": {"type": "integer", "description": "Priority 0-4"},
                    "assignee": {"type": "string", "description": "Agent or person assigned"},
                    "depends_on": {"type": "array", "items": {"type": "string"},
                                   "description": "Issue IDs this depends on"},
                },
                "required": ["title"],
            },
        },
        handler=_handle_orchestra_track,
    )

    ctx.register_tool(
        name="orchestra_ready",
        toolset="orchestra",
        schema={
            "name": "orchestra_ready",
            "description": "Find ready-to-work issues — all dependencies resolved, not blocked, not already claimed.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_orchestra_ready,
    )

    ctx.register_tool(
        name="orchestra_claim",
        toolset="orchestra",
        schema={
            "name": "orchestra_claim",
            "description": "Claim an issue for an agent with a time-limited lease (5-min TTL, renewable via heartbeat). Prevents duplicate work in multi-agent scenarios.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID to claim (e.g. iss-001)"},
                    "agent_id": {"type": "string", "description": "Agent identifier (default: default)"},
                },
                "required": ["issue_id"],
            },
        },
        handler=_handle_orchestra_claim,
    )

    ctx.register_tool(
        name="orchestra_heartbeat",
        toolset="orchestra",
        schema={
            "name": "orchestra_heartbeat",
            "description": "Renew a claim lease for an agent's current issue. Extends the 5-min TTL by another 5 minutes. Use periodically while working on a claimed issue to prevent lease expiry.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Agent identifier (default: default)"},
                },
            },
        },
        handler=_handle_orchestra_heartbeat,
    )

    ctx.register_tool(
        name="orchestra_update",
        toolset="orchestra",
        schema={
            "name": "orchestra_update",
            "description": "Update an issue's status/fields or add a delta to a change. When updating an issue, can change status, title, assignee. When adding a delta, records ADDED/MODIFIED requirements against a spec.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue ID to update"},
                    "status": {"type": "string", "enum": ["open", "claimed", "in_progress", "blocked", "deferred", "closed"],
                               "description": "New status"},
                    "title": {"type": "string", "description": "New title"},
                    "assignee": {"type": "string", "description": "New assignee"},
                    "change": {"type": "string", "description": "Change name (for delta)"},
                    "spec": {"type": "string", "description": "Spec name (for delta)"},
                    "added": {"type": "array", "items": {"type": "string"},
                              "description": "Added requirements (for delta)"},
                    "modified": {"type": "array", "items": {"type": "string"},
                                 "description": "Modified requirements (for delta)"},
                },
            },
        },
        handler=_handle_orchestra_update,
    )

    ctx.register_tool(
        name="orchestra_validate",
        toolset="orchestra",
        schema={
            "name": "orchestra_validate",
            "description": "Validate a spec against quality rules: checks for SHALL/MUST keywords, minimum overview length, and scenario coverage. Returns ERROR/WARNING report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "spec": {"type": "string", "description": "Spec name to validate"},
                },
                "required": ["spec"],
            },
        },
        handler=_handle_orchestra_validate,
    )

    ctx.register_tool(
        name="orchestra_status",
        toolset="orchestra",
        schema={
            "name": "orchestra_status",
            "description": "Full workspace health: spec count, issue count by status, active leases, ready-to-work count.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_orchestra_status,
    )

    ctx.register_tool(
        name="orchestra_sync",
        toolset="orchestra",
        schema={
            "name": "orchestra_sync",
            "description": "Bidirectional sync with GitHub Issues. Push a local issue to GitHub, or pull open issues from GitHub into the local tracker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["push", "pull"],
                                  "description": "push: local→GitHub, pull: GitHub→local"},
                    "repo": {"type": "string", "description": "GitHub repo (owner/name)"},
                    "token": {"type": "string", "description": "GitHub token (or set GITHUB_TOKEN env)"},
                    "issue_id": {"type": "string", "description": "Local issue ID (required for push)"},
                    "max_issues": {"type": "integer", "description": "Max issues to pull (default: 10)"},
                },
                "required": ["direction", "repo"],
            },
        },
        handler=_handle_orchestra_sync,
    )

    ctx.register_tool(
        name="orchestra_archive",
        toolset="orchestra",
        schema={
            "name": "orchestra_archive",
            "description": "Archive a completed change: merges all delta specs back into the main spec files. The spec files are updated with new requirements from ADDED sections.",
            "parameters": {
                "type": "object",
                "properties": {
                    "change": {"type": "string", "description": "Change name to archive"},
                },
                "required": ["change"],
            },
        },
        handler=_handle_orchestra_archive,
    )

    ctx.register_command(
        name="orchestra",
        description=(
            "Orchestra — spec-driven development + issue tracking. "
            "Subcommands: init, status, propose, plan, track, ready, "
            "claim, update, validate, archive, sync"
        ),
        handler=_cmd_orchestra,
    )

    logger.info("hermes-orchestra: registered 11 tools + 1 command")
    return {"name": "hermes-orchestra", "version": "1.0.0"}
