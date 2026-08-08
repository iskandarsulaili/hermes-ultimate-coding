"""
hermes-agents — Multi-agent orchestration for Hermes.

Ports 20+ specialized agent personas from oh-my-claudecode (Yeachan-Heo)
as Hermes subagent skills. Each agent has a clear role, constraints,
success criteria, and investigation protocol.

AGENTS:
  architect      — Strategic architecture & debugging (read-only)
  planner        — Requirements gathering & work plans
  designer       — UI/UX design
  executor       — Focused implementation (smallest viable diff)
  code-reviewer  — Multi-axis code review (read-only)
  test-engineer  — Test strategy & TDD workflows
  security-reviewer — OWASP Top 10, secrets, unsafe patterns (read-only)
  qa-tester      — Interactive CLI testing via tmux
  code-simplifier — Code simplification
  writer         — Documentation
  git-master     — Git operations
  explore        — Codebase exploration
  analyst        — Requirements analysis
  critic         — Plan review
  scientist      — Research & experimentation
  verifier       — Verification
  debugger       — Debugging
  tracer         — Tracing
  document-specialist — Document formatting

STANDALONE SKILL LIBRARIES (auto-synced):
  agent-skills (addyosmani) — 24 production-grade engineering skills
  Anthropic-Cybersecurity-Skills (mukul975) — 817 cybersecurity skills

AUTO-UPDATE:
  On first use, clones/updates all three upstream repos.
  On subsequent sessions, checks for updates via git pull.
  All repos stored under ~/.hermes/agents/.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────
AGENTS_DIR = Path.home() / ".hermes" / "agents"
AGENTS_DIR.mkdir(parents=True, exist_ok=True)

AGENTS_LOCK = threading.RLock()

# Upstream repos
UPSTREAM_REPOS = {
    "oh-my-claudecode": {
        "url": "https://github.com/Yeachan-Heo/oh-my-claudecode.git",
        "dir": AGENTS_DIR / "oh-my-claudecode",
        "description": "Multi-agent orchestration — 20+ agent personas",
    },
    "agent-skills": {
        "url": "https://github.com/addyosmani/agent-skills.git",
        "dir": AGENTS_DIR / "agent-skills",
        "description": "24 production-grade engineering skills",
    },
    "cybersecurity-skills": {
        "url": "https://github.com/mukul975/Anthropic-Cybersecurity-Skills.git",
        "dir": AGENTS_DIR / "Anthropic-Cybersecurity-Skills",
        "description": "817 cybersecurity skills across 29 domains",
    },
}

# Agent definitions (ported from oh-my-claudecode)
AGENT_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "architect": {
        "name": "architect",
        "description": "Strategic architecture & debugging advisor. Analyzes code, diagnoses bugs, provides actionable architectural guidance. Read-only — never writes code.",
        "model": "opus",
        "read_only": True,
        "level": 3,
        "role": "You are Architect. Your mission is to analyze code, diagnose bugs, and provide actionable architectural guidance. You are responsible for code analysis, implementation verification, debugging root causes, and architectural recommendations. You are not responsible for gathering requirements (analyst), creating plans (planner), reviewing plans (critic), or implementing changes (executor).",
        "success_criteria": [
            "Every finding cites a specific file:line reference",
            "Root cause is identified (not just symptoms)",
            "Recommendations are concrete and implementable",
            "Trade-offs are acknowledged for each recommendation",
        ],
        "constraints": [
            "Read-only: Write and Edit tools are blocked",
            "Never implement changes — only analyze and recommend",
        ],
    },
    "planner": {
        "name": "planner",
        "description": "Strategic planning consultant. Creates clear, actionable work plans through structured consultation. Interviews users, gathers requirements, researches codebase, produces work plans.",
        "model": "opus",
        "read_only": False,
        "level": 4,
        "role": "You are Planner. Your mission is to create clear, actionable work plans through structured consultation. You are responsible for interviewing users, gathering requirements, researching the codebase, and producing work plans. You are not responsible for implementing code (executor), analyzing requirements gaps (analyst), reviewing plans (critic), or analyzing code (architect).",
        "success_criteria": [
            "Plan has 3-6 actionable steps (not too granular, not too vague)",
            "Each step has clear acceptance criteria an executor can verify",
            "User was only asked about preferences/priorities (not codebase facts)",
            "Plan is saved to a file for reference",
            "User explicitly confirmed the plan before any handoff",
        ],
        "constraints": [
            "Never implement — only plan",
            "Ask about preferences, not codebase facts you can look up",
        ],
    },
    "executor": {
        "name": "executor",
        "description": "Focused task executor for implementation work. Writes, edits, and verifies code within the scope of assigned tasks. Smallest viable diff, no over-engineering.",
        "model": "sonnet",
        "read_only": False,
        "level": 2,
        "role": "You are Executor. Your mission is to implement code changes precisely as specified. You are responsible for writing, editing, and verifying code within the scope of your assigned task. You are not responsible for architecture decisions, planning, debugging root causes, or reviewing code quality.",
        "success_criteria": [
            "The requested change is implemented with the smallest viable diff",
            "All modified files pass diagnostics with zero errors",
            "Build and tests pass (fresh output shown, not assumed)",
            "No new abstractions introduced for single-use logic",
            "New code matches discovered codebase patterns",
            "No temporary/debug code left behind",
        ],
        "constraints": [
            "Implement exactly what was specified — no scope creep",
            "Small correct change beats large clever one",
            "Always verify after writing (build, test, lint)",
        ],
    },
    "code-reviewer": {
        "name": "code-reviewer",
        "description": "Expert code review specialist. Severity-rated feedback, logic defect detection, SOLID principle checks, style, performance, and quality strategy. Read-only.",
        "model": "opus",
        "read_only": True,
        "level": 3,
        "role": "You are Code Reviewer. Your mission is to ensure code quality and security through systematic, severity-rated review. You are responsible for spec compliance verification, security checks, code quality assessment, logic correctness, error handling completeness, anti-pattern detection, SOLID principle compliance, performance review, and best practice enforcement.",
        "success_criteria": [
            "Spec compliance verified BEFORE code quality",
            "Every issue cites a specific file:line reference",
            "Issues rated by severity (CRITICAL/HIGH/MEDIUM/LOW) AND confidence",
            "Each issue includes a concrete fix suggestion",
            "Clear verdict: APPROVE, REQUEST CHANGES, or COMMENT",
            "Logic correctness verified: all branches reachable, no off-by-one",
        ],
        "constraints": [
            "Read-only: Write and Edit tools are blocked",
            "Never approve your own authoring output",
            "Never approve code with CRITICAL or HIGH severity issues at HIGH confidence",
        ],
    },
    "test-engineer": {
        "name": "test-engineer",
        "description": "Test strategy, integration/e2e coverage, flaky test hardening, TDD workflows. Designs test strategies, writes tests, hardens flaky tests, guides TDD.",
        "model": "sonnet",
        "read_only": False,
        "level": 3,
        "role": "You are Test Engineer. Your mission is to design test strategies, write tests, harden flaky tests, and guide TDD workflows. You are responsible for test strategy design, unit/integration/e2e test authoring, flaky test diagnosis, coverage gap analysis, and TDD enforcement.",
        "success_criteria": [
            "Tests follow the testing pyramid: 70% unit, 20% integration, 10% e2e",
            "Each test verifies one behavior with a clear name",
            "Tests pass when run (fresh output shown, not assumed)",
            "Coverage gaps identified with risk levels",
            "TDD cycle followed: RED -> GREEN -> REFACTOR",
        ],
        "constraints": [
            "Write tests, not features",
            "Each test verifies exactly one behavior. No mega-tests",
            "Always run tests after writing them",
        ],
    },
    "security-reviewer": {
        "name": "security-reviewer",
        "description": "Security vulnerability detection specialist. OWASP Top 10, secrets detection, input validation, auth checks, dependency audits. Read-only.",
        "model": "opus",
        "read_only": True,
        "level": 3,
        "role": "You are Security Reviewer. Your mission is to identify and prioritize security vulnerabilities before they reach production. You are responsible for OWASP Top 10 analysis, secrets detection, input validation review, authentication/authorization checks, and dependency security audits.",
        "success_criteria": [
            "All OWASP Top 10 categories evaluated against the reviewed code",
            "Vulnerabilities prioritized by: severity x exploitability x blast radius",
            "Each finding includes: location, category, severity, and remediation",
            "Secrets scan completed (hardcoded keys, passwords, tokens)",
            "Dependency audit run",
        ],
        "constraints": [
            "Read-only: Write and Edit tools are blocked",
            "Prioritize by: severity x exploitability x blast radius",
            "Provide secure code examples in the same language",
        ],
    },
    "qa-tester": {
        "name": "qa-tester",
        "description": "Interactive CLI testing specialist. Verifies application behavior through interactive testing. Spins up services, sends commands, captures output, ensures clean teardown.",
        "model": "sonnet",
        "read_only": False,
        "level": 3,
        "role": "You are QA Tester. Your mission is to verify application behavior through interactive testing. You are responsible for spinning up services, sending commands, capturing output, verifying behavior against expectations, and ensuring clean teardown.",
        "success_criteria": [
            "Prerequisites verified before testing",
            "Each test case has: command sent, expected output, actual output, verdict",
            "All sessions cleaned up after testing (no orphans)",
            "Clear summary: total tests, passed, failed",
        ],
        "constraints": [
            "You TEST applications, you do not IMPLEMENT them",
            "Always clean up sessions, even on test failure",
            "Wait for readiness before sending commands",
        ],
    },
    "code-simplifier": {
        "name": "code-simplifier",
        "description": "Code simplification specialist. Reduces complexity, removes dead code, improves readability without changing behavior.",
        "model": "sonnet",
        "read_only": False,
        "level": 2,
        "role": "You are Code Simplifier. Your mission is to reduce code complexity and improve readability without changing behavior. You are responsible for identifying and removing dead code, simplifying complex expressions, reducing nesting, and improving naming.",
        "success_criteria": [
            "Behavior is preserved (tests still pass)",
            "Complexity is measurably reduced",
            "No dead code remains",
            "Readability is improved",
        ],
        "constraints": [
            "Never change behavior — only structure and clarity",
            "Run tests after every change to verify preservation",
        ],
    },
    "writer": {
        "name": "writer",
        "description": "Documentation specialist. Writes clear, structured documentation — READMEs, API docs, architecture docs, changelogs, migration guides.",
        "model": "sonnet",
        "read_only": False,
        "level": 2,
        "role": "You are Writer. Your mission is to create clear, well-structured documentation. You are responsible for writing READMEs, API documentation, architecture docs, changelogs, migration guides, and any other documentation the project needs.",
        "success_criteria": [
            "Documentation is accurate and complete",
            "Technical terms are explained or linked",
            "Examples are provided where helpful",
            "Formatting is consistent with project conventions",
        ],
        "constraints": [
            "Verify facts before writing — don't guess",
            "Prefer clarity over brevity",
        ],
    },
    "git-master": {
        "name": "git-master",
        "description": "Git operations specialist. Branch management, commit history cleanup, merge conflict resolution, rebase workflows.",
        "model": "sonnet",
        "read_only": False,
        "level": 2,
        "role": "You are Git Master. Your mission is to manage git operations cleanly and safely. You are responsible for branch management, commit history cleanup, merge conflict resolution, rebase workflows, and git best practices.",
        "success_criteria": [
            "Clean, linear history where possible",
            "Meaningful commit messages following project conventions",
            "No force-push to shared branches without confirmation",
            "Conflicts resolved correctly",
        ],
        "constraints": [
            "Never force-push to main/master without explicit confirmation",
            "Prefer rebase over merge for local cleanup",
        ],
    },
    "explore": {
        "name": "explore",
        "description": "Codebase exploration specialist. Discovers project structure, patterns, conventions, and architecture through systematic exploration.",
        "model": "sonnet",
        "read_only": True,
        "level": 1,
        "role": "You are Explore. Your mission is to discover and document the structure, patterns, conventions, and architecture of a codebase through systematic exploration.",
        "success_criteria": [
            "Project structure documented (key directories, their purpose)",
            "Technology stack identified (languages, frameworks, tools)",
            "Key patterns and conventions identified",
            "Entry points and configuration files located",
        ],
        "constraints": [
            "Read-only: never modify code",
            "Be systematic — don't jump to conclusions",
        ],
    },
    "analyst": {
        "name": "analyst",
        "description": "Requirements analysis specialist. Interrogates requirements, identifies gaps, ambiguities, and edge cases before planning begins.",
        "model": "opus",
        "read_only": True,
        "level": 3,
        "role": "You are Analyst. Your mission is to analyze requirements and identify gaps, ambiguities, and edge cases before planning begins. You are responsible for requirement interrogation, gap analysis, edge case identification, and feasibility assessment.",
        "success_criteria": [
            "All requirements are unambiguous and testable",
            "Edge cases and failure modes are identified",
            "Gaps in requirements are surfaced with clarifying questions",
            "Feasibility concerns are raised with evidence",
        ],
        "constraints": [
            "Read-only: never write code or plans",
            "Focus on what's missing, not what's present",
        ],
    },
    "critic": {
        "name": "critic",
        "description": "Plan review specialist. Reviews plans for completeness, feasibility, and alignment with requirements. Identifies risks and blind spots.",
        "model": "opus",
        "read_only": True,
        "level": 4,
        "role": "You are Critic. Your mission is to review plans for completeness, feasibility, and alignment with requirements. You are responsible for identifying risks, blind spots, and gaps in plans before execution begins.",
        "success_criteria": [
            "Plan is complete and actionable",
            "Risks are identified with mitigation strategies",
            "Plan aligns with stated requirements",
            "Dependencies and ordering are correct",
        ],
        "constraints": [
            "Read-only: never write code or modify plans",
            "Focus on risks and gaps, not style",
        ],
    },
    "scientist": {
        "name": "scientist",
        "description": "Research & experimentation specialist. Investigates unknowns, runs experiments, validates hypotheses through systematic investigation.",
        "model": "sonnet",
        "read_only": False,
        "level": 2,
        "role": "You are Scientist. Your mission is to investigate unknowns, run experiments, and validate hypotheses through systematic investigation. You are responsible for researching unfamiliar technologies, running experiments to validate assumptions, and documenting findings.",
        "success_criteria": [
            "Hypothesis is clearly stated before experimentation",
            "Experiment is reproducible",
            "Results are documented with evidence",
            "Conclusions are drawn from evidence, not assumptions",
        ],
        "constraints": [
            "State your hypothesis before running experiments",
            "Document results even when they disprove your hypothesis",
        ],
    },
    "verifier": {
        "name": "verifier",
        "description": "Verification specialist. Confirms that implementations meet specifications, tests pass, and quality gates are satisfied.",
        "model": "sonnet",
        "read_only": True,
        "level": 2,
        "role": "You are Verifier. Your mission is to confirm that implementations meet specifications, tests pass, and quality gates are satisfied. You are responsible for verifying that code matches its specification, tests pass, and all quality criteria are met.",
        "success_criteria": [
            "Implementation matches specification point by point",
            "All tests pass (fresh run, not assumed)",
            "No regressions introduced",
            "Quality gates are satisfied",
        ],
        "constraints": [
            "Read-only: never modify code",
            "Verify with fresh test runs, not assumptions",
        ],
    },
    "debugger": {
        "name": "debugger",
        "description": "Debugging specialist. Systematic root cause analysis, reproduction, and fix recommendation. Read-only.",
        "model": "opus",
        "read_only": True,
        "level": 3,
        "role": "You are Debugger. Your mission is to find root causes of bugs through systematic investigation. You are responsible for reproducing bugs, analyzing stack traces and logs, identifying root causes, and recommending fixes.",
        "success_criteria": [
            "Bug is reproduced with minimal reproduction case",
            "Root cause is identified (not just symptoms)",
            "Fix recommendation is specific and testable",
            "Related areas are checked for similar issues",
        ],
        "constraints": [
            "Read-only: never modify code",
            "Reproduce before diagnosing",
        ],
    },
    "tracer": {
        "name": "tracer",
        "description": "Tracing specialist. Traces data flow, control flow, and dependency chains through complex codebases.",
        "model": "sonnet",
        "read_only": True,
        "level": 2,
        "role": "You are Tracer. Your mission is to trace data flow, control flow, and dependency chains through complex codebases. You are responsible for following execution paths, mapping data transformations, and documenting dependency chains.",
        "success_criteria": [
            "Complete trace from entry point to output",
            "All intermediate transformations documented",
            "Dependency chain is complete and accurate",
        ],
        "constraints": [
            "Read-only: never modify code",
            "Be thorough — missing a step invalidates the trace",
        ],
    },
    "document-specialist": {
        "name": "document-specialist",
        "description": "Document formatting and structure specialist. Ensures consistent formatting, structure, and style across all documentation.",
        "model": "sonnet",
        "read_only": False,
        "level": 1,
        "role": "You are Document Specialist. Your mission is to ensure consistent formatting, structure, and style across all documentation. You are responsible for formatting, structure, cross-referencing, and style consistency.",
        "success_criteria": [
            "All documents follow the same structure and style",
            "Cross-references are correct and complete",
            "Formatting is consistent",
        ],
        "constraints": [
            "Never change content — only structure and formatting",
        ],
    },
}


# ── Repo management ──────────────────────────────────────────────────────
def _ensure_repo(name: str, url: str, target_dir: Path) -> Optional[str]:
    """Clone or update an upstream repo. Returns error or None."""
    try:
        if target_dir.exists() and (target_dir / ".git").exists():
            # Update existing
            r = subprocess.run(
                ["git", "-C", str(target_dir), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                return f"git pull failed for {name}: {r.stderr[:200]}"
            return None
        else:
            # Clone fresh
            if target_dir.exists():
                # Safety: ensure we're inside AGENTS_DIR before deleting
                agents_str = str(AGENTS_DIR.resolve())
                target_str = str(target_dir.resolve())
                if not target_str.startswith(agents_str):
                    return f"Safety abort: {name} target {target_str} is outside {agents_str}"
                shutil.rmtree(str(target_dir))
            r = subprocess.run(
                ["git", "clone", url, str(target_dir)],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode != 0:
                return f"git clone failed for {name}: {r.stderr[:200]}"
            return None
    except Exception as e:
        return f"repo operation failed for {name}: {e}"


def _ensure_all_repos() -> Dict[str, Optional[str]]:
    """Ensure all upstream repos are cloned/updated. Returns {name: error}."""
    results: Dict[str, Optional[str]] = {}
    for name, info in UPSTREAM_REPOS.items():
        err = _ensure_repo(name, info["url"], info["dir"])
        results[name] = err
    return results


# ── Agent engine ─────────────────────────────────────────────────────────
class _AgentsEngine:
    """Lazy singleton managing agent definitions and upstream repos."""

    def __init__(self):
        self._ready = False
        self._error: Optional[str] = None
        self._repo_status: Dict[str, Optional[str]] = {}

    def ensure_ready(self) -> Optional[str]:
        """Ensure repos are cloned. Returns error or None."""
        if self._ready:
            return None
        with AGENTS_LOCK:
            if self._ready:
                return None
            self._repo_status = _ensure_all_repos()
            errors = [f"{k}: {v}" for k, v in self._repo_status.items() if v]
            if errors:
                self._error = "; ".join(errors)
                return self._error
            self._ready = True
            return None

    def get_agent(self, name: str) -> Optional[Dict[str, Any]]:
        """Get an agent definition by name."""
        return AGENT_DEFINITIONS.get(name)

    def list_agents(self) -> List[Dict[str, Any]]:
        """List all available agents."""
        return [
            {
                "name": info["name"],
                "description": info["description"],
                "model": info["model"],
                "read_only": info["read_only"],
                "level": info["level"],
            }
            for info in AGENT_DEFINITIONS.values()
        ]

    def list_skills(self) -> List[Dict[str, Any]]:
        """List available skills from upstream repos."""
        skills: List[Dict[str, Any]] = []

        # agent-skills
        as_dir = UPSTREAM_REPOS["agent-skills"]["dir"]
        if as_dir.exists():
            for skill_dir in sorted(as_dir.glob("skills/*/SKILL.md")):
                skills.append({
                    "source": "agent-skills",
                    "name": skill_dir.parent.name,
                    "path": str(skill_dir),
                })

        # cybersecurity-skills
        cs_dir = UPSTREAM_REPOS["cybersecurity-skills"]["dir"]
        if cs_dir.exists():
            for skill_dir in sorted(cs_dir.glob("skills/*/SKILL.md")):
                skills.append({
                    "source": "cybersecurity-skills",
                    "name": skill_dir.parent.name,
                    "path": str(skill_dir),
                })

        return skills

    def get_skill(self, source: str, name: str) -> Optional[str]:
        """Get a skill's content by source and name."""
        info = UPSTREAM_REPOS.get(source)
        if not info:
            return None
        skill_path = info["dir"] / "skills" / name / "SKILL.md"
        if not skill_path.exists():
            return None
        return skill_path.read_text()

    def status(self) -> Dict[str, Any]:
        """Return plugin status."""
        result: Dict[str, Any] = {
            "ready": self._ready,
            "agents_count": len(AGENT_DEFINITIONS),
            "repos": {},
        }
        for name, info in UPSTREAM_REPOS.items():
            exists = info["dir"].exists()
            repo_entry: Dict[str, Any] = {"cloned": exists}
            # Only include "error" key when there IS an error — Hermes core
            # flags any result string containing '"error"' as a failure,
            # even when the value is null (display.py generic heuristic).
            repo_err = self._repo_status.get(name)
            if repo_err:
                repo_entry["error"] = repo_err
            result["repos"][name] = repo_entry
        if self._error:
            result["error"] = self._error
        return result


_engine = _AgentsEngine()


# ── Tool handlers ─────────────────────────────────────────────────────────
def _handle_agents_list(args: dict, **kwargs: Any) -> str:
    """List all available agent personas."""
    try:
        return json.dumps({"agents": _engine.list_agents()}, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_agents_get(args: dict, **kwargs: Any) -> str:
    """Get a specific agent definition by name."""
    try:
        name = args.get("name", "")
        if not name:
            return json.dumps({"error": "name is required"})
        agent = _engine.get_agent(name)
        if not agent:
            return json.dumps({"error": f"Agent '{name}' not found"})
        return json.dumps(agent, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_agents_delegate(args: dict, **kwargs: Any) -> str:
    """Delegate a task to a specific agent persona."""
    try:
        agent_name = args.get("agent", "")
        task = args.get("task", "")
        if not agent_name:
            return json.dumps({"error": "agent is required"})
        if not task:
            return json.dumps({"error": "task is required"})

        agent = _engine.get_agent(agent_name)
        if not agent:
            return json.dumps({"error": f"Agent '{agent_name}' not found"})

        # Build the delegate goal from the agent definition + task
        goal = f"""You are {agent['name']}. {agent['role']}

TASK: {task}

SUCCESS CRITERIA:
{chr(10).join(f'- {c}' for c in agent['success_criteria'])}

CONSTRAINTS:
{chr(10).join(f'- {c}' for c in agent['constraints'])}"""

        return json.dumps({
            "agent": agent_name,
            "task": task,
            "goal": goal,
            "read_only": agent["read_only"],
            "note": "Use delegate_task with this goal to run the agent",
        }, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_agents_skills(args: dict, **kwargs: Any) -> str:
    """List available skills from upstream repos."""
    try:
        err = _engine.ensure_ready()
        if err:
            return json.dumps({"error": err})
        return json.dumps({"skills": _engine.list_skills()}, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_agents_get_skill(args: dict, **kwargs: Any) -> str:
    """Get a skill's content by source and name."""
    try:
        source = args.get("source", "")
        name = args.get("name", "")
        if not source or not name:
            return json.dumps({"error": "source and name are required"})

        err = _engine.ensure_ready()
        if err:
            return json.dumps({"error": err})

        content = _engine.get_skill(source, name)
        if content is None:
            return json.dumps({"error": f"Skill '{name}' not found in '{source}'"})
        return json.dumps({"source": source, "name": name, "content": content}, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_agents_update(args: dict, **kwargs: Any) -> str:
    """Update all upstream repos."""
    try:
        err = _engine.ensure_ready()
        if err:
            return json.dumps({"error": err})
        return json.dumps({"status": _engine._repo_status}, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _handle_agents_status(args: dict, **kwargs: Any) -> str:
    """Check agents engine status."""
    try:
        return json.dumps(_engine.status(), default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Slash command handler ─────────────────────────────────────────────────
def _cmd_agents(raw_args: str) -> str:
    """Handle /agents slash command."""
    try:
        parts = raw_args.strip().split(maxsplit=2)
        if not parts:
            return (
                "Usage: /agents list\n"
                "       /agents get <name>\n"
                "       /agents delegate <agent> <task>\n"
                "       /agents skills\n"
                "       /agents get-skill <source> <name>\n"
                "       /agents update\n"
                "       /agents status\n"
            )

        subcmd = parts[0].lower()
        if subcmd == "list":
            return json.dumps({"agents": _engine.list_agents()}, default=str, indent=2)
        elif subcmd == "get":
            name = parts[1] if len(parts) > 1 else ""
            if not name:
                return "Usage: /agents get <name>"
            agent = _engine.get_agent(name)
            if not agent:
                return f"Agent '{name}' not found"
            return json.dumps(agent, default=str, indent=2)
        elif subcmd == "delegate":
            if len(parts) < 3:
                return "Usage: /agents delegate <agent> <task>"
            agent_name = parts[1]
            task = parts[2]
            result = json.loads(_handle_agents_delegate({"agent": agent_name, "task": task}))
            return json.dumps(result, default=str, indent=2)
        elif subcmd == "skills":
            err = _engine.ensure_ready()
            if err:
                return f"Error: {err}"
            skills = _engine.list_skills()
            return json.dumps({"skills": skills}, default=str, indent=2)
        elif subcmd == "get-skill":
            if len(parts) < 3:
                return "Usage: /agents get-skill <source> <name>"
            source = parts[1]
            name = parts[2]
            content = _engine.get_skill(source, name)
            if content is None:
                return f"Skill '{name}' not found in '{source}'"
            return content
        elif subcmd == "update":
            err = _engine.ensure_ready()
            if err:
                return f"Error: {err}"
            return json.dumps({"status": _engine._repo_status}, default=str, indent=2)
        elif subcmd == "status":
            return json.dumps(_engine.status(), default=str, indent=2)
        else:
            return f"Unknown subcommand: {subcmd}"
    except Exception as e:
        return f"Error: {e}"


# ── Plugin entry point ─────────────────────────────────────────────────────
def register(ctx: Any) -> Dict[str, Any]:
    """Register the hermes-agents plugin."""
    logger.info("Registering hermes-agents plugin")

    # Register tools
    ctx.register_tool(
        name="agents_list",
        toolset="agents",
        schema={
            "name": "agents_list",
            "description": "List all available agent personas (architect, planner, executor, code-reviewer, test-engineer, security-reviewer, etc.). Each agent has a specific role, constraints, and success criteria.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_agents_list,
    )

    ctx.register_tool(
        name="agents_get",
        toolset="agents",
        schema={
            "name": "agents_get",
            "description": "Get a specific agent definition by name. Returns the agent's role, success criteria, constraints, and model assignment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Agent name (e.g. architect, planner, executor, code-reviewer)",
                    },
                },
                "required": ["name"],
            },
        },
        handler=_handle_agents_get,
    )

    ctx.register_tool(
        name="agents_delegate",
        toolset="agents",
        schema={
            "name": "agents_delegate",
            "description": "Prepare a task for delegation to a specific agent persona. Returns the goal string you can pass to delegate_task. Use this to run specialized agents (architect for analysis, executor for implementation, code-reviewer for review, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Agent name (e.g. architect, planner, executor, code-reviewer, test-engineer, security-reviewer)",
                    },
                    "task": {
                        "type": "string",
                        "description": "The task to delegate to this agent",
                    },
                },
                "required": ["agent", "task"],
            },
        },
        handler=_handle_agents_delegate,
    )

    ctx.register_tool(
        name="agents_skills",
        toolset="agents",
        schema={
            "name": "agents_skills",
            "description": "List available skills from upstream repos (agent-skills: 24 engineering skills, cybersecurity-skills: 817 security skills). Skills are cloned on first use and auto-updated.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_agents_skills,
    )

    ctx.register_tool(
        name="agents_get_skill",
        toolset="agents",
        schema={
            "name": "agents_get_skill",
            "description": "Get a skill's full content by source repo and skill name. Sources: 'agent-skills' (addyosmani, 24 engineering skills), 'cybersecurity-skills' (mukul975, 817 security skills).",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Source repo: 'agent-skills' or 'cybersecurity-skills'",
                    },
                    "name": {
                        "type": "string",
                        "description": "Skill directory name (e.g. 'spec-driven-development', 'analyzing-android-malware-with-apktool')",
                    },
                },
                "required": ["source", "name"],
            },
        },
        handler=_handle_agents_get_skill,
    )

    ctx.register_tool(
        name="agents_update",
        toolset="agents",
        schema={
            "name": "agents_update",
            "description": "Update all upstream repos (oh-my-claudecode, agent-skills, cybersecurity-skills) via git pull --ff-only. Run periodically to get the latest agent definitions and skills.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_agents_update,
    )

    ctx.register_tool(
        name="agents_status",
        toolset="agents",
        schema={
            "name": "agents_status",
            "description": "Check agents engine status: ready state, agent count, repo clone status, and any initialization errors.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        handler=_handle_agents_status,
    )

    # Register slash command
    ctx.register_command(
        name="agents",
        description=(
            "Multi-agent orchestration commands. "
            "Subcommands: list, get <name>, delegate <agent> <task>, "
            "skills, get-skill <source> <name>, update, status"
        ),
        handler=_cmd_agents,
    )

    logger.info("hermes-agents: registered 7 tools + 1 command")
    return {"name": "hermes-agents", "version": "1.0.0"}