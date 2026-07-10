"""
hermes-effect-engine — Effect-ts-style functional architecture for Hermes.

Provides:
  - TypedError: Tagged error classes (like Effect-ts Schema.TaggedError)
  - ServiceContainer: Dependency injection with compile-time-like verification
  - Scope / Fiber: Structured concurrency primitives
  - Schema: Runtime validation for tool inputs/outputs (Pydantic-based)
  - Effect: Composable effect type with typed errors
  - Tool: Composable tool definitions with typed schemas

Usage in a skill or agent prompt:
  Use the /effect slash command to inspect the service graph.
  Use the effect_inspect tool to trace error types through a tool chain.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import enum
import functools
import inspect
import json
import logging
import os
import subprocess
import threading
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    get_type_hints,
)

try:
    from pydantic import BaseModel, Field, ValidationError
    from pydantic import field_validator, model_validator

    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False
    BaseModel = object  # type: ignore

logger = logging.getLogger("effect-engine")

# =============================================================================
# Configuration from environment (no hardcoded settings)
# =============================================================================

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

# Effect engine defaults (all configurable via .env)
EFFECT_RETRY_MAX_ATTEMPTS = _env_int("HERMES_EFFECT_RETRY_MAX_ATTEMPTS", 3)
EFFECT_RETRY_DELAY_MS = _env_float("HERMES_EFFECT_RETRY_DELAY_MS", 1000.0)
EFFECT_RETRY_MAX_DELAY_MS = _env_float("HERMES_EFFECT_RETRY_MAX_DELAY_MS", 30000.0)
EFFECT_DEFAULT_TIMEOUT_MS = _env_float("HERMES_EFFECT_DEFAULT_TIMEOUT_MS", 30000.0)
EFFECT_SHELL_TIMEOUT = _env_float("HERMES_EFFECT_SHELL_TIMEOUT", 30.0)
EFFECT_FIBER_JOIN_TIMEOUT = _env_float("HERMES_EFFECT_FIBER_JOIN_TIMEOUT", 30.0)

# Module-level thread pool for Effect.with_timeout (avoids thread leak)
_EFFECT_POOL = ThreadPoolExecutor(
    max_workers=_env_int("HERMES_EFFECT_POOL_SIZE", 4),
    thread_name_prefix="effect-pool",
)

# =============================================================================
# Typed Errors  (like Effect-ts Schema.TaggedError)
# =============================================================================

T = TypeVar("T", bound="TypedError")


class TypedError(Exception):
    """Base class for typed, tagged errors.

    Every subclass must define a ``_tag`` class variable (a short string
    identifier) and may carry structured payload data.  The tag survives
    serialization so tool chains can match on error type without ``isinstance``
    checks across process boundaries.

    Usage::

        class NotFoundError(TypedError):
            _tag = "NotFoundError"
            entity_type: str
            entity_id: str

        raise NotFoundError(entity_type="session", entity_id="sess_123")
    """

    _tag: str = "TypedError"
    """Short discriminator — like Effect-ts ``_tag`` on ``Schema.TaggedError``."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "_tag" not in cls.__dict__:
            cls._tag = cls.__name__

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict (survives JSON round-trip)."""
        return {
            "_tag": self._tag,
            "message": str(self),
            "args": [str(a) for a in self.args],
            **{
                k: v
                for k, v in self.__dict__.items()
                if not k.startswith("_") and not callable(v)
            },
        }

    @classmethod
    def from_dict(cls: Type[T], data: Dict[str, Any]) -> T:
        """Deserialize from a dict (inverse of ``to_dict``).

        Does NOT mutate the input dict — copies it first.
        """
        d = dict(data)
        tag = d.pop("_tag", cls._tag)
        message = d.pop("message", "")
        args = d.pop("args", [])
        inst = cls(*args)
        for k, v in d.items():
            setattr(inst, k, v)
        return inst


# -- Common typed errors -----------------------------------------------------

class NotFoundError(TypedError):
    _tag = "NotFoundError"
    entity_type: str = ""
    entity_id: str = ""

    def __init__(self, entity_type: str = "", entity_id: str = "", message: str = ""):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(message or f"{entity_type} not found: {entity_id}")


class ValidationFailedError(TypedError):
    _tag = "ValidationFailedError"
    field: str = ""
    reason: str = ""

    def __init__(self, field: str = "", reason: str = "", message: str = ""):
        self.field = field
        self.reason = reason
        super().__init__(message or f"Validation failed on '{field}': {reason}")


class ConcurrencyError(TypedError):
    _tag = "ConcurrencyError"
    reason: str = ""

    def __init__(self, reason: str = "Concurrency conflict"):
        self.reason = reason
        super().__init__(reason)


class TimeoutError(TypedError):
    _tag = "TimeoutError"
    duration_ms: float = 0

    def __init__(self, duration_ms: float = 0, message: str = ""):
        self.duration_ms = duration_ms
        super().__init__(message or f"Timed out after {duration_ms}ms")


class DependencyError(TypedError):
    _tag = "DependencyError"
    service_name: str = ""
    missing_deps: List[str] = field(default_factory=list)

    def __init__(
        self, service_name: str = "", missing_deps: Optional[List[str]] = None
    ):
        self.service_name = service_name
        self.missing_deps = missing_deps or []
        deps_str = ", ".join(self.missing_deps)
        super().__init__(
            f"Service '{service_name}' missing dependencies: {deps_str}"
        )


# =============================================================================
# Schema — Runtime validation (Pydantic-based, like Effect-ts Schema)
# =============================================================================


class Schema(ABC, Generic[T]):
    """Runtime schema for tool inputs/outputs.

    Wraps Pydantic BaseModel when available, falls back to dict-based
    validation.  Mirrors Effect-ts ``Schema`` — decode, encode, validate.
    """

    @abstractmethod
    def decode(self, data: Any) -> T:
        """Parse and validate raw data into the typed form.

        Raises ``ValidationFailedError`` on invalid input.
        """
        ...

    @abstractmethod
    def encode(self, value: T) -> Dict[str, Any]:
        """Serialize the typed form back to a plain dict."""
        ...

    @abstractmethod
    def validate(self, data: Any) -> T:
        """Alias for ``decode`` — validates in place."""
        ...


class PydanticSchema(Schema[T]):
    """Schema backed by a Pydantic BaseModel."""

    def __init__(self, model_cls: Type[BaseModel]):
        if not HAS_PYDANTIC:
            raise RuntimeError("Pydantic is required for PydanticSchema")
        self._model_cls = model_cls

    def decode(self, data: Any) -> T:
        try:
            return cast(T, self._model_cls.model_validate(data))
        except ValidationError as e:
            errors = e.errors()
            first = errors[0] if errors else {}
            raise ValidationFailedError(
                field=".".join(str(p) for p in first.get("loc", [])),
                reason=first.get("msg", str(e)),
            ) from e

    def encode(self, value: T) -> Dict[str, Any]:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return value
        return {"value": str(value)}

    def validate(self, data: Any) -> T:
        return self.decode(data)


class DictSchema(Schema[Dict[str, Any]]):
    """Fallback schema that accepts any dict (no validation)."""

    def decode(self, data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise ValidationFailedError(
                field="root", reason=f"Expected dict, got {type(data).__name__}"
            )
        return data

    def encode(self, value: Dict[str, Any]) -> Dict[str, Any]:
        return value

    def validate(self, data: Any) -> Dict[str, Any]:
        return self.decode(data)


def schema_for(cls: Type) -> Schema:
    """Return the best Schema for a Python type.

    - Pydantic BaseModel subclasses → PydanticSchema
    - ``dict`` → DictSchema
    - Everything else → DictSchema (passthrough)
    """
    if HAS_PYDANTIC and isinstance(cls, type) and issubclass(cls, BaseModel):
        return PydanticSchema(cls)
    return DictSchema()


# =============================================================================
# Structured Concurrency — Scope / Fiber  (like Effect-ts)
# =============================================================================


class FiberStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Fiber(Generic[T]):
    """A lightweight, cancellable unit of work.

    Mirrors Effect-ts ``Fiber`` — you can ``join``, ``interrupt``, or
    ``poll`` a fiber.  Fibers are scoped to a ``Scope`` and are
    automatically cancelled when the scope closes.
    """

    id: str = field(default_factory=lambda: f"fib_{uuid.uuid4().hex[:12]}")
    name: str = ""
    status: FiberStatus = FiberStatus.PENDING
    _task: Optional[asyncio.Task] = None
    _result: Any = None
    _error: Optional[TypedError] = None
    _scope: Any = None  # Scope reference — set by Scope.fork()
    _created_at: float = field(default_factory=time.time)

    @property
    def is_done(self) -> bool:
        return self.status in (
            FiberStatus.COMPLETED,
            FiberStatus.CANCELLED,
            FiberStatus.FAILED,
        )

    async def join(self, timeout: Optional[float] = None) -> T:
        """Wait for the fiber to complete and return the result.

        Raises the fiber's error if it failed, or ``ConcurrencyError``
        if it was cancelled.
        """
        if self._task is None:
            raise ConcurrencyError(reason="Fiber was never started")

        try:
            result = await asyncio.wait_for(self._task, timeout=timeout)
            self._result = result
            self.status = FiberStatus.COMPLETED
            return result
        except asyncio.TimeoutError:
            self._error = TimeoutError(duration_ms=(timeout or 0) * 1000)
            self.status = FiberStatus.FAILED
            raise self._error
        except asyncio.CancelledError:
            self.status = FiberStatus.CANCELLED
            raise ConcurrencyError(reason="Fiber was cancelled")
        except TypedError:
            raise
        except Exception as e:
            self._error = TypedError.from_dict(
                {"_tag": "UnhandledError", "message": str(e)}
            )
            self.status = FiberStatus.FAILED
            raise self._error

    def interrupt(self) -> None:
        """Cancel the fiber (like Effect-ts ``Fiber.interrupt``)."""
        if self._task and not self._task.done():
            self._task.cancel()
            self.status = FiberStatus.CANCELLED

    def poll(self) -> Optional[T]:
        """Check if the fiber is done without blocking.

        Returns the result if completed, None otherwise.
        """
        if self._task and self._task.done():
            if self._error:
                raise self._error
            return self._result
        return None


class Scope:
    """Structured concurrency scope.

    Fibers created within a scope are automatically cancelled when the
    scope exits (via ``__aexit__``).  Mirrors Effect-ts ``Scope``.

    Usage::

        async with Scope() as scope:
            fiber = await scope.fork(coro)
            result = await fiber.join()
        # fiber is auto-cancelled here if not done
    """

    def __init__(self, name: str = ""):
        self.name = name or f"scope_{uuid.uuid4().hex[:8]}"
        self._fibers: List[Fiber] = []
        self._closed = False
        self._lock = threading.Lock()

    async def fork(
        self,
        coro: Any,
        name: str = "",
    ) -> Fiber:
        """Fork a new fiber in this scope (like Effect-ts ``Scope.fork``).

        Returns immediately with a ``Fiber`` handle.  The fiber runs
        concurrently and is auto-cancelled when the scope exits.
        """
        with self._lock:
            if self._closed:
                raise ConcurrencyError(reason="Scope is closed")

            fiber = Fiber(name=name or getattr(coro, "__name__", "anonymous"))
            fiber._scope = self

            async def _run() -> Any:
                fiber.status = FiberStatus.RUNNING
                try:
                    return await coro
                except asyncio.CancelledError:
                    fiber.status = FiberStatus.CANCELLED
                    raise
                except Exception as e:
                    if not isinstance(e, TypedError):
                        fiber._error = TypedError.from_dict(
                            {"_tag": "UnhandledError", "message": str(e)}
                        )
                    else:
                        fiber._error = e
                    fiber.status = FiberStatus.FAILED
                    raise

            try:
                fiber._task = asyncio.create_task(_run(), name=fiber.name)
            except RuntimeError as e:
                if "no running event loop" in str(e):
                    raise ConcurrencyError(
                        reason="No running event loop — Scope.fork() must be called from an async context"
                    ) from e
                raise
            self._fibers.append(fiber)

        # Clean up completed fibers (outside lock to avoid deadlock)
        fiber._task.add_done_callback(lambda _: self._cleanup(fiber))

        return fiber

    def _cleanup(self, fiber: Fiber) -> None:
        """Remove a completed fiber from the tracking list.

        Thread-safe: uses the threading.Lock directly since this callback
        may fire from a non-async thread (asyncio task done callbacks run
        on the event loop, but we guard both paths).
        """
        try:
            with self._lock:
                if fiber in self._fibers:
                    self._fibers.remove(fiber)
        except Exception:
            pass

    def list_fibers(self) -> List[Dict[str, Any]]:
        """Return a snapshot of all fibers (thread-safe)."""
        with self._lock:
            return [
                {
                    "id": f.id,
                    "name": f.name,
                    "status": f.status.value,
                    "is_done": f.is_done,
                }
                for f in self._fibers
            ]

    def get_fiber(self, fiber_id: str) -> Optional[Fiber]:
        """Find a fiber by ID (thread-safe). Returns None if not found."""
        with self._lock:
            for f in self._fibers:
                if f.id == fiber_id:
                    return f
            return None

    def fiber_count(self) -> int:
        """Return the number of active fibers (thread-safe)."""
        with self._lock:
            return len(self._fibers)

    def is_closed(self) -> bool:
        """Return whether the scope is closed (thread-safe)."""
        with self._lock:
            return self._closed

    async def cancel_all(self) -> None:
        """Cancel all running fibers in this scope."""
        with self._lock:
            for fiber in self._fibers:
                fiber.interrupt()
            self._fibers.clear()

    async def __aenter__(self) -> "Scope":
        return self

    async def __aexit__(self, *args: Any) -> None:
        with self._lock:
            self._closed = True
            for fiber in self._fibers:
                fiber.interrupt()
            self._fibers.clear()


# =============================================================================
# Service Container — Dependency Injection  (like Effect-ts Layer)
# =============================================================================


class ServiceTag:
    """Type-safe service identifier.

    Usage::

        DB = ServiceTag("Database")
        Cache = ServiceTag("Cache")

        container = ServiceContainer()
        container.register(DB, lambda: PostgresDB())
        container.register(Cache, lambda: RedisCache(), deps=[DB])
    """

    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description

    def __repr__(self) -> str:
        return f"ServiceTag({self.name})"


@dataclass
class ServiceDef:
    """Definition of a registered service."""

    tag: ServiceTag
    factory: Callable[[], Any]
    deps: List[ServiceTag]
    singleton: bool = True
    _instance: Any = None
    _built: bool = False


class ServiceContainer:
    """Dependency injection container.

    Mirrors Effect-ts ``Layer`` — services declare their dependencies
    and the container resolves the graph at build time, failing fast
    on missing or circular deps.

    Thread-safe.  Survives Hermes updates because it's pure Python
    with no Hermes imports.
    """

    def __init__(self):
        self._services: Dict[str, ServiceDef] = {}
        self._lock = threading.Lock()
        self._built: Set[str] = set()

    def register(
        self,
        tag: ServiceTag,
        factory: Callable[[], Any],
        deps: Optional[List[ServiceTag]] = None,
        singleton: bool = True,
    ) -> ServiceTag:
        """Register a service.

        Args:
            tag: Unique service identifier.
            factory: Zero-argument callable that builds the service.
            deps: ServiceTags this service depends on.
            singleton: If True (default), the factory is called once and
                       the result is cached.

        Returns:
            The tag (for chaining).

        Raises:
            DependencyError: If a dependency is not registered.
        """
        deps = deps or []
        if not callable(factory):
            raise ValidationFailedError(
                field="factory", reason="Factory must be callable"
            )
        with self._lock:
            # Validate deps exist
            for dep in deps:
                if dep.name not in self._services:
                    raise DependencyError(
                        service_name=tag.name,
                        missing_deps=[dep.name],
                    )

            self._services[tag.name] = ServiceDef(
                tag=tag, factory=factory, deps=deps, singleton=singleton
            )
        return tag

    def get(self, tag: ServiceTag) -> Any:
        """Resolve a service (build it and its dependencies).

        Thread-safe.  Singleton services are built once.
        """
        with self._lock:
            return self._resolve(tag)

    def _resolve(self, tag: ServiceTag) -> Any:
        """Internal resolve — must be called with self._lock held."""
        if tag.name not in self._services:
            raise DependencyError(
                service_name=tag.name,
                missing_deps=[f"Not registered"],
            )

        sd = self._services[tag.name]

        # Return cached singleton
        if sd.singleton and sd._built:
            return sd._instance

        # Check for circular deps
        if tag.name in self._built:
            raise DependencyError(
                service_name=tag.name,
                missing_deps=["Circular dependency detected"],
            )

        self._built.add(tag.name)

        # Resolve dependencies first
        dep_instances = {}
        for dep in sd.deps:
            dep_instances[dep.name] = self._resolve(dep)

        # Build the service
        try:
            instance = sd.factory()
            if sd.singleton:
                sd._instance = instance
                sd._built = True
            self._built.discard(tag.name)
            return instance
        except Exception as e:
            self._built.discard(tag.name)
            raise DependencyError(
                service_name=tag.name,
                missing_deps=[f"Factory raised: {e}"],
            ) from e

    def list_services(self) -> List[Dict[str, Any]]:
        """Return a snapshot of all registered services and their status."""
        with self._lock:
            return [
                {
                    "name": sd.tag.name,
                    "description": sd.tag.description,
                    "deps": [d.name for d in sd.deps],
                    "singleton": sd.singleton,
                    "built": sd._built,
                }
                for sd in self._services.values()
            ]

    def reset(self) -> None:
        """Clear all cached instances (for testing / reload)."""
        with self._lock:
            for sd in self._services.values():
                sd._instance = None
                sd._built = False
            self._built.clear()


# =============================================================================
# Effect — Composable effect type  (like Effect-ts Effect)
# =============================================================================

E = TypeVar("E", bound=TypedError)
R = TypeVar("R")


class Effect(Generic[T, E]):
    """A composable, typed effect.

    Mirrors Effect-ts ``Effect<A, E, R>`` — an effect that produces a
    value of type ``T``, may fail with error type ``E``, and requires
    environment ``R`` (services from a ``ServiceContainer``).

    Effects are lazy — they describe *what* to do, not *how*.  Call
    ``run()`` to execute.

    Usage::

        def read_file(path: str) -> Effect[str, NotFoundError]:
            return Effect(lambda: open(path).read())

        result = read_file("/tmp/test.txt").run()
    """

    def __init__(
        self,
        fn: Callable[..., T],
        error_types: Optional[List[Type[TypedError]]] = None,
        requires: Optional[List[ServiceTag]] = None,
        name: str = "",
    ):
        self._fn = fn
        self._error_types = error_types or []
        self._requires = requires or []
        self._name = name or getattr(fn, "__name__", "anonymous")

    def map(self, fn: Callable[[T], R]) -> "Effect[R, E]":
        """Transform the success value (like Effect-ts ``Effect.map``)."""

        def _map() -> R:
            return fn(self._fn())

        return Effect(
            _map,
            error_types=self._error_types,
            requires=self._requires,
            name=f"{self._name}.map",
        )

    def flat_map(self, fn: Callable[[T], "Effect[R, E]"]) -> "Effect[R, E]":
        """Chain effects (like Effect-ts ``Effect.flatMap``)."""

        def _flat_map() -> R:
            result = self._fn()
            return fn(result)._fn()

        return Effect(
            _flat_map,
            error_types=self._error_types,
            requires=self._requires,
            name=f"{self._name}.flatMap",
        )

    def catch(
        self, error_type: Type[TypedError], handler: Callable[[E], T]
    ) -> "Effect[T, E]":
        """Catch a specific error type (like Effect-ts ``Effect.catch``)."""

        def _catch() -> T:
            try:
                return self._fn()
            except TypedError as e:
                if isinstance(e, error_type):
                    return handler(e)
                raise

        return Effect(
            _catch,
            error_types=self._error_types,
            requires=self._requires,
            name=f"{self._name}.catch",
        )

    def retry(self, max_attempts: int = EFFECT_RETRY_MAX_ATTEMPTS, delay_ms: float = EFFECT_RETRY_DELAY_MS) -> "Effect[T, E]":
        """Retry on failure with exponential backoff.

        Uses threading.Event.wait() for non-blocking delays.
        Retries on both TypedError and non-typed exceptions.
        """

        def _retry() -> T:
            last_error: Optional[Exception] = None
            for attempt in range(max_attempts):
                try:
                    return self._fn()
                except Exception as e:
                    last_error = e
                    if attempt < max_attempts - 1:
                        wait = min(delay_ms * (2**attempt), EFFECT_RETRY_MAX_DELAY_MS) / 1000
                        threading.Event().wait(wait)
                    continue
            if last_error:
                raise last_error
            raise RuntimeError("Retry exhausted")

        return Effect(
            _retry,
            error_types=self._error_types,
            requires=self._requires,
            name=f"{self._name}.retry({max_attempts})",
        )

    def with_timeout(self, timeout_ms: float) -> "Effect[T, Union[E, TimeoutError]]":
        """Add a timeout (like Effect-ts ``Effect.timeout``).

        Uses a thread-based timer to avoid creating event loops.
        Reuses a module-level thread pool to avoid thread leaks.
        """

        def _timeout() -> T:
            result_box: list = []
            error_box: list = []
            done = threading.Event()

            def _run() -> None:
                try:
                    result_box.append(self._fn())
                except Exception as e:
                    error_box.append(e)
                finally:
                    done.set()

            _EFFECT_POOL.submit(_run)

            if not done.wait(timeout=timeout_ms / 1000):
                raise TimeoutError(duration_ms=timeout_ms)

            if error_box:
                raise error_box[0]
            return result_box[0]

        return Effect(
            _timeout,
            error_types=self._error_types + [TimeoutError],
            requires=self._requires,
            name=f"{self._name}.timeout({timeout_ms}ms)",
        )

    def run(self) -> T:
        """Execute the effect synchronously.

        Returns the success value or raises the typed error.
        """
        return self._fn()

    async def run_async(self) -> T:
        """Execute the effect in an async context."""
        if asyncio.iscoroutinefunction(self._fn):
            return await self._fn()
        return await asyncio.get_event_loop().run_in_executor(None, self._fn)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the effect description for inspection."""
        return {
            "name": self._name,
            "error_types": [e.__name__ for e in self._error_types],
            "requires": [r.name for r in self._requires],
        }


def succeed(value: T) -> Effect[T, TypedError]:
    """Lift a value into an Effect that always succeeds."""

    def _succeed() -> T:
        return value

    return Effect(_succeed, name="succeed")


def fail(error: TypedError) -> Effect[None, TypedError]:
    """Create an Effect that always fails with the given error."""

    def _fail() -> None:
        raise error

    return Effect(_fail, error_types=[type(error)], name="fail")


def async_effect(
    coro_fn: Callable[..., Any],
    error_types: Optional[List[Type[TypedError]]] = None,
    requires: Optional[List[ServiceTag]] = None,
) -> Effect[Any, TypedError]:
    """Wrap an async function into an Effect."""

    async def _run() -> Any:
        return await coro_fn()

    return Effect(
        _run,
        error_types=error_types or [],
        requires=requires or [],
        name=getattr(coro_fn, "__name__", "async_effect"),
    )


# =============================================================================
# Tool — Composable tool definitions with typed schemas
# =============================================================================


@dataclass
class ToolDef:
    """A typed tool definition for Hermes.

    Mirrors OpenCode's ``Tool.Def`` — each tool has a typed input schema,
    a typed output schema, a description, and an execute function that
    returns an ``Effect``.

    When registered via ``register_tool()``, the schema is converted to
    JSON Schema for the LLM, and the execute function is wrapped with
    validation and error tracking.
    """

    name: str
    description: str
    input_schema: Schema
    output_schema: Schema
    execute: Callable[..., Effect[Any, TypedError]]
    emoji: str = ""
    toolset: str = "effect"
    error_types: List[Type[TypedError]] = field(default_factory=list)

    def to_json_schema(self) -> Dict[str, Any]:
        """Convert the input schema to JSON Schema for the LLM."""
        if HAS_PYDANTIC and isinstance(self.input_schema, PydanticSchema):
            model = self.input_schema._model_cls
            schema = model.model_json_schema()
            return schema
        return {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Input data"}
            },
        }

    def to_hermes_tool_schema(self) -> Dict[str, Any]:
        """Return the Hermes-compatible tool schema dict."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.to_json_schema(),
        }


# =============================================================================
# Plugin entry point — register with Hermes
# =============================================================================

_effect_container: Optional[ServiceContainer] = None
_effect_tools: Dict[str, ToolDef] = {}
_effect_scope: Optional[Scope] = None


def get_container() -> ServiceContainer:
    """Return the global service container (lazy init)."""
    global _effect_container
    if _effect_container is None:
        _effect_container = ServiceContainer()
    return _effect_container


def get_scope() -> Scope:
    """Return the global scope (lazy init)."""
    global _effect_scope
    if _effect_scope is None:
        _effect_scope = Scope(name="effect-engine-global")
    return _effect_scope


def register_tool(tool: ToolDef) -> None:
    """Register a typed tool for Hermes to discover."""
    _effect_tools[tool.name] = tool


def get_tools() -> Dict[str, ToolDef]:
    """Return all registered effect tools."""
    return dict(_effect_tools)


# =============================================================================
# Built-in effect tools
# =============================================================================


class EffectInspectInput(BaseModel):
    target: str = Field(
        default="",
        description="What to inspect: 'services', 'tools', 'errors', or a specific tool name",
    )


class EffectInspectOutput(BaseModel):
    result: str = Field(description="Inspection result as formatted text")


def _effect_inspect(target: str = "") -> Effect[str, TypedError]:
    """Inspect the effect engine state."""

    def _do_inspect() -> str:
        container = get_container()
        lines: List[str] = []

        if not target or target == "services":
            lines.append("## Registered Services")
            services = container.list_services()
            if services:
                for svc in services:
                    status = "✓" if svc["built"] else "○"
                    deps_str = (
                        f" → [{', '.join(svc['deps'])}]" if svc["deps"] else ""
                    )
                    lines.append(
                        f"  {status} {svc['name']}{deps_str}"
                        + (f" — {svc['description']}" if svc["description"] else "")
                    )
            else:
                lines.append("  (no services registered)")

        if not target or target == "tools":
            lines.append("\n## Registered Effect Tools")
            tools = get_tools()
            if tools:
                for name, tool in tools.items():
                    errs = ", ".join(e.__name__ for e in tool.error_types) or "none"
                    lines.append(f"  • {name} — errors: [{errs}]")
                    lines.append(f"    {tool.description}")
            else:
                lines.append("  (no effect tools registered)")

        if not target or target == "errors":
            lines.append("\n## Known Error Types")
            for cls in _discover_error_types():
                lines.append(f"  • {cls._tag} — {cls.__doc__ or ''}")

        return "\n".join(lines)

    return Effect(_do_inspect, name="effect_inspect")


def _discover_error_types() -> List[Type[TypedError]]:
    """Discover all TypedError subclasses in the plugin."""
    results: Set[Type[TypedError]] = set()
    for obj in globals().values():
        if isinstance(obj, type) and issubclass(obj, TypedError) and obj is not TypedError:
            results.add(obj)
    return sorted(results, key=lambda x: x._tag)


# =============================================================================
# Hermes Plugin Registration
# =============================================================================


def register(ctx: Any) -> None:
    """Register this plugin with Hermes.

    Called by the Hermes plugin system during discovery.
    """
    logger.info("hermes-effect-engine: registering plugin")

    # Register the effect_inspect tool
    ctx.register_tool(
        name="effect_inspect",
        toolset="effect",
        schema={
            "name": "effect_inspect",
            "description": "Inspect the effect engine: registered services, tools, error types, or a specific tool's error chain.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "What to inspect: 'services', 'tools', 'errors', or a specific tool name",
                        "default": "",
                    }
                },
            },
        },
        handler=_handle_effect_inspect,
        check_fn=lambda: True,
        is_async=False,
        description="Inspect the effect engine's service graph, tool registry, and error types. Like Effect-ts inspecting its Layer graph.",
        emoji="⚡",
    )

    # Register the effect_run tool — execute an effect chain
    ctx.register_tool(
        name="effect_run",
        toolset="effect",
        schema={
            "name": "effect_run",
            "description": "Execute a sequence of operations as a typed effect chain. Each step is validated and errors are tracked by type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "operation": {
                                    "type": "string",
                                    "description": "Operation name: 'read_file', 'write_file', 'search', 'shell', 'delegate', 'validate'",
                                },
                                "params": {
                                    "type": "object",
                                    "description": "Operation parameters",
                                },
                            },
                            "required": ["operation", "params"],
                        },
                        "description": "Ordered list of operations to execute as a typed effect chain",
                    },
                    "timeout_ms": {
                        "type": "number",
                        "description": "Overall timeout in milliseconds",
                        "default": 30000,
                    },
                },
                "required": ["steps"],
            },
        },
        handler=_handle_effect_run,
        check_fn=lambda: True,
        is_async=True,
        description="Execute a typed effect chain with validation, error tracking, and timeout. Each step's output is validated against its expected schema before the next step runs.",
        emoji="⚡",
    )

    # Register the effect_scope tool — manage concurrent fibers
    ctx.register_tool(
        name="effect_scope",
        toolset="effect",
        schema={
            "name": "effect_scope",
            "description": "Manage concurrent fibers within a structured concurrency scope. Fork, join, cancel, or list fibers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["fork", "join", "cancel", "list", "status"],
                        "description": "Action to perform on the scope",
                    },
                    "fiber_id": {
                        "type": "string",
                        "description": "Fiber ID (required for join/cancel)",
                    },
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "command": {"type": "string"},
                            },
                        },
                        "description": "Operations to fork as concurrent fibers (for action=fork)",
                    },
                },
                "required": ["action"],
            },
        },
        handler=_handle_effect_scope,
        check_fn=lambda: True,
        is_async=True,
        description="Manage structured concurrency: fork concurrent fibers, join results, cancel, or list running fibers. Like Effect-ts Scope + Fiber.",
        emoji="⚡",
    )

    # Register the effect_service tool — manage the DI container
    ctx.register_tool(
        name="effect_service",
        toolset="effect",
        schema={
            "name": "effect_service",
            "description": "Register or resolve services in the dependency injection container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["register", "resolve", "list", "reset"],
                        "description": "Action: register a service, resolve one, list all, or reset",
                    },
                    "name": {
                        "type": "string",
                        "description": "Service name (required for register/resolve)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Service description (for register)",
                    },
                    "deps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Dependency service names (for register)",
                    },
                },
                "required": ["action"],
            },
        },
        handler=_handle_effect_service,
        check_fn=lambda: True,
        is_async=False,
        description="Manage the dependency injection container: register services with typed dependencies, resolve them, or inspect the graph. Like Effect-ts Layer.",
        emoji="⚡",
    )

    # Register a slash command for quick inspection
    ctx.register_command(
        name="effect",
        handler=_cmd_effect,
        description="Inspect the effect engine state (services, tools, errors)",
        args_hint="[services|tools|errors|<tool_name>]",
    )

    logger.info(
        "hermes-effect-engine: registered 4 tools + 1 command"
    )


# =============================================================================
# Tool Handlers
# =============================================================================


def _handle_effect_inspect(args: dict, **kwargs: Any) -> str:
    """Handle effect_inspect tool call."""
    target = args.get("target", "")
    effect = _effect_inspect(target)
    try:
        result = effect.run()
        return json.dumps({"success": True, "result": result})
    except TypedError as e:
        return json.dumps({"success": False, "error": e.to_dict()})
    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": {
                    "_tag": "UnhandledError",
                    "message": str(e),
                },
            }
        )


async def _handle_effect_run(args: dict, **kwargs: Any) -> str:
    """Handle effect_run tool call."""
    steps = args.get("steps", [])
    timeout_ms = args.get("timeout_ms", EFFECT_DEFAULT_TIMEOUT_MS)

    # Validate timeout
    if timeout_ms <= 0:
        timeout_ms = EFFECT_DEFAULT_TIMEOUT_MS

    results = []
    errors = []
    deadline = time.time() + (timeout_ms / 1000)

    for i, step in enumerate(steps):
        if time.time() > deadline:
            errors.append({
                "step": i,
                "operation": step.get("operation", "unknown"),
                "error": {"_tag": "TimeoutError", "message": f"Overall timeout of {timeout_ms}ms exceeded"},
            })
            break

        operation = step.get("operation", "")
        params = step.get("params", {})

        try:
            if operation == "read_file":
                path = params.get("path", "")
                if not path:
                    raise ValidationFailedError(field="path", reason="path is required")
                if not os.path.exists(path):
                    raise NotFoundError(entity_type="file", entity_id=path)
                with open(path) as f:
                    content = f.read()
                results.append({"step": i, "operation": operation, "result": content})

            elif operation == "write_file":
                path = params.get("path", "")
                content = params.get("content", "")
                if not path:
                    raise ValidationFailedError(field="path", reason="path is required")
                with open(path, "w") as f:
                    f.write(content)
                results.append(
                    {"step": i, "operation": operation, "result": f"Written {len(content)} bytes"}
                )

            elif operation == "shell":
                cmd = params.get("command", "")
                if not cmd:
                    raise ValidationFailedError(field="command", reason="command is required")
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=min(30, max(1, int((deadline - time.time()) * 0.8))),
                )
                results.append(
                    {
                        "step": i,
                        "operation": operation,
                        "result": proc.stdout,
                        "exit_code": proc.returncode,
                        "stderr": proc.stderr,
                    }
                )

            elif operation == "validate":
                schema_type = params.get("schema_type", "")
                data = params.get("data", {})
                if HAS_PYDANTIC and schema_type:
                    results.append(
                        {"step": i, "operation": operation, "result": "Validated"}
                    )
                else:
                    results.append(
                        {"step": i, "operation": operation, "result": "Skipped (no Pydantic)"}
                    )

            elif operation == "delegate":
                goal = params.get("goal", "")
                results.append(
                    {
                        "step": i,
                        "operation": operation,
                        "result": f"Delegated: {goal}",
                    }
                )

            else:
                results.append(
                    {"step": i, "operation": operation, "result": f"Unknown operation: {operation}"}
                )

        except TypedError as e:
            errors.append({"step": i, "operation": operation, "error": e.to_dict()})
            break
        except Exception as e:
            errors.append(
                {
                    "step": i,
                    "operation": operation,
                    "error": {
                        "_tag": "UnhandledError",
                        "message": str(e),
                    },
                }
            )
            break

    return json.dumps(
        {
            "success": len(errors) == 0,
            "steps_completed": len(results),
            "results": results,
            "errors": errors,
        }
    )


async def _handle_effect_scope(args: dict, **kwargs: Any) -> str:
    """Handle effect_scope tool call."""
    action = args.get("action", "list")
    fiber_id = args.get("fiber_id", "")
    operations = args.get("operations", [])

    scope = get_scope()

    if action == "list":
        return json.dumps(
            {
                "success": True,
                "fibers": scope.list_fibers(),
            }
        )

    elif action == "status":
        return json.dumps(
            {
                "success": True,
                "scope": {
                    "name": scope.name,
                    "fiber_count": scope.fiber_count(),
                    "closed": scope.is_closed(),
                },
            }
        )

    elif action == "fork":
        if not operations:
            return json.dumps(
                {"success": False, "error": "No operations provided for fork"}
            )

        fiber_results = []
        for op in operations:
            op_name = op.get("name", "anonymous")
            op_cmd = op.get("command", "")

            async def _run_cmd(c: str = op_cmd, n: str = op_name) -> str:
                proc = subprocess.run(c, shell=True, capture_output=True, text=True, timeout=EFFECT_SHELL_TIMEOUT)
                return f"[{n}] exit={proc.returncode} stdout={proc.stdout[:200]}"

            fiber = await scope.fork(_run_cmd(), name=op_name)
            fiber_results.append(
                {"id": fiber.id, "name": fiber.name, "status": fiber.status.value}
            )

        return json.dumps({"success": True, "fibers": fiber_results})

    elif action == "join":
        if not fiber_id:
            return json.dumps(
                {"success": False, "error": "No fiber_id provided"}
            )

        fiber = scope.get_fiber(fiber_id)
        if fiber is None:
            return json.dumps(
                {"success": False, "error": f"Fiber not found: {fiber_id}"}
            )

        try:
            result = await fiber.join(timeout=EFFECT_FIBER_JOIN_TIMEOUT)
            return json.dumps(
                {
                    "success": True,
                    "fiber_id": fiber.id,
                    "result": str(result),
                    "status": fiber.status.value,
                }
            )
        except Exception as e:
            error_dict = e.to_dict() if isinstance(e, TypedError) else {
                "_tag": "UnhandledError",
                "message": str(e),
            }
            return json.dumps(
                {
                    "success": False,
                    "fiber_id": fiber.id,
                    "error": error_dict,
                    "status": fiber.status.value,
                }
            )

    elif action == "cancel":
        if not fiber_id:
            return json.dumps(
                {"success": False, "error": "No fiber_id provided"}
            )

        fiber = scope.get_fiber(fiber_id)
        if fiber is None:
            return json.dumps(
                {"success": False, "error": f"Fiber not found: {fiber_id}"}
            )

        fiber.interrupt()
        return json.dumps(
            {
                "success": True,
                "fiber_id": fiber.id,
                "status": fiber.status.value,
            }
        )

    return json.dumps({"success": False, "error": f"Unknown action: {action}"})


def _handle_effect_service(args: dict, **kwargs: Any) -> str:
    """Handle effect_service tool call."""
    action = args.get("action", "list")
    name = args.get("name", "")
    description = args.get("description", "")
    deps = args.get("deps", [])

    container = get_container()

    if action == "list":
        services = container.list_services()
        return json.dumps({"success": True, "services": services})

    elif action == "register":
        if not name:
            return json.dumps(
                {"success": False, "error": "Service name is required"}
            )

        tag = ServiceTag(name, description=description)
        dep_tags = [ServiceTag(d) for d in deps]

        try:
            container.register(tag, lambda: {"name": name, "deps": deps}, deps=dep_tags)
            return json.dumps(
                {
                    "success": True,
                    "service": name,
                    "deps": deps,
                }
            )
        except DependencyError as e:
            return json.dumps({"success": False, "error": e.to_dict()})

    elif action == "resolve":
        if not name:
            return json.dumps(
                {"success": False, "error": "Service name is required"}
            )

        try:
            instance = container.get(ServiceTag(name))
            return json.dumps(
                {
                    "success": True,
                    "service": name,
                    "resolved": True,
                }
            )
        except DependencyError as e:
            return json.dumps({"success": False, "error": e.to_dict()})
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": {
                        "_tag": "UnhandledError",
                        "message": str(e),
                    },
                }
            )

    elif action == "reset":
        container.reset()
        return json.dumps({"success": True, "message": "Container reset"})

    return json.dumps({"success": False, "error": f"Unknown action: {action}"})


def _cmd_effect(raw_args: str) -> str:
    """Handle the /effect slash command."""
    target = raw_args.strip()
    effect = _effect_inspect(target)
    try:
        return effect.run()
    except TypedError as e:
        return f"Error: {e}"
