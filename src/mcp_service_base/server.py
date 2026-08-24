"""ServiceServer — the MCP scaffolding that ties the contract together.

A service creates one ``ServiceServer``, registers its event types and its
read/act tools, and calls ``run()``. The core wires in the durable log, delivery
fan-out, policy gate, and telemetry so the service only writes domain code.

MCP capabilities exposed to the agent (walkthrough §3):
- describe   — event types, payload schemas, available tools.
- subscribe  — event name + condition + callback address.
- read tools — broadly exposed (registered via @read_tool).
- act tools  — narrowly allow-listed and gated (registered via @act_tool).

The ``mcp`` SDK is imported lazily inside :meth:`to_mcp`, so the rest of the
library (emit, describe, log, policy) works and is testable without it installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .delivery import Delivery, DisabledSink
from .envelope import EventEnvelope, new_event
from .log import DurableLog, SQLiteLog
from .policy import ActionSpec, GateLevel, PolicyGate
from .telemetry import Telemetry


@dataclass
class _Tool:
    name: str
    fn: Callable[..., Any]
    description: str
    schema: dict[str, Any]


@dataclass
class _Subscription:
    event_type: str
    condition: str
    callback_url: str


@dataclass
class ServiceServer:
    """One per service. Domain code registers tools; the core does the plumbing."""

    service: str
    store_id: str
    log: DurableLog = field(default=None)  # type: ignore[assignment]
    delivery: Delivery = field(default=None)  # type: ignore[assignment]
    policy: PolicyGate = field(default_factory=PolicyGate)
    telemetry: Telemetry = field(default=None)  # type: ignore[assignment]

    _event_types: dict[str, dict] = field(default_factory=dict)
    _read_tools: dict[str, _Tool] = field(default_factory=dict)
    _act_tools: dict[str, _Tool] = field(default_factory=dict)
    _subscriptions: list[_Subscription] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.log is None:
            self.log = SQLiteLog(service=self.service)
        if self.delivery is None:
            self.delivery = Delivery(sinks=[DisabledSink()])
        if self.telemetry is None:
            self.telemetry = Telemetry(service=self.service)

    # -- registration -----------------------------------------------------

    def register_event_type(self, name: str, schema: dict[str, Any]) -> None:
        self._event_types[name] = schema

    def read_tool(
        self, name: str, description: str = "", schema: dict | None = None
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a broadly-exposed read tool."""

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._read_tools[name] = _Tool(name, fn, description, schema or {})
            return fn

        return deco

    def act_tool(
        self,
        name: str,
        level: GateLevel,
        description: str = "",
        schema: dict | None = None,
        max_calls: int | None = None,
        per_seconds: float = 60.0,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register an action tool and its policy-gate level + rate limit."""

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._act_tools[name] = _Tool(name, fn, description, schema or {})
            self.policy.register(
                ActionSpec(
                    name=name,
                    level=level,
                    max_calls=max_calls,
                    per_seconds=per_seconds,
                )
            )
            return fn

        return deco

    # -- runtime ----------------------------------------------------------

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        ref_id: str | None = None,
    ) -> EventEnvelope:
        """Emit-to-log-first, then fan out to enabled sinks (walkthrough §4)."""
        event = new_event(event_type, self.service, self.store_id, payload, ref_id)
        with self.telemetry.span(f"emit:{event_type}"):
            self.log.append(event)
            self.delivery.dispatch(event)
        return event

    def call_action(self, name: str, **args: Any) -> dict[str, Any]:
        """Run an act tool THROUGH the policy gate. The only path to acting."""
        decision = self.policy.evaluate(name, args)
        if not decision.allowed:
            return {
                "executed": False,
                "level": decision.level.value,
                "reason": decision.reason,
            }
        with self.telemetry.span(f"act:{name}"):
            result = self._act_tools[name].fn(**args)
        return {"executed": True, "level": decision.level.value, "result": result}

    def subscribe(self, event_type: str, condition: str, callback_url: str) -> None:
        self._subscriptions.append(_Subscription(event_type, condition, callback_url))

    def describe(self) -> dict[str, Any]:
        """Self-description rich enough for a coding agent to use unassisted."""
        return {
            "service": self.service,
            "store_id": self.store_id,
            "event_types": self._event_types,
            "read_tools": {
                t.name: {"description": t.description, "schema": t.schema}
                for t in self._read_tools.values()
            },
            "act_tools": {
                t.name: {
                    "description": t.description,
                    "schema": t.schema,
                    "gate": self.policy._actions[t.name].level.value,
                }
                for t in self._act_tools.values()
            },
        }

    # -- MCP binding (lazy import) ----------------------------------------

    def to_mcp(self):  # noqa: ANN201 - returns an MCP server instance
        """Build an MCP server exposing describe/subscribe/read/act.

        Works with both the current SDK (``mcp>=2`` exposes ``MCPServer``) and the
        1.x line (``mcp.server.fastmcp.FastMCP``). Both share the same
        ``.tool()`` decorator and ``.run()`` API.

        Requires the ``mcp`` extra: ``pip install mcp-service-base[mcp]``.
        """
        try:
            from mcp.server import MCPServer as _Server  # mcp >= 2.0
        except ImportError:
            try:
                from mcp.server.fastmcp import FastMCP as _Server  # mcp 1.x
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise RuntimeError(
                    "The MCP layer needs the 'mcp' package. "
                    "Install with: pip install mcp-service-base[mcp]"
                ) from exc

        app = _Server(self.service)

        @app.tool(name="describe", description="Describe this service's contract.")
        def _describe() -> dict:
            return self.describe()

        @app.tool(name="subscribe", description="Subscribe to an event type.")
        def _subscribe(event_type: str, condition: str, callback_url: str) -> dict:
            self.subscribe(event_type, condition, callback_url)
            return {"subscribed": event_type, "condition": condition}

        for tool in self._read_tools.values():
            app.tool(name=tool.name, description=tool.description)(tool.fn)

        # Action tools are exposed but routed through the gate, never called raw.
        for tool in self._act_tools.values():
            self._bind_gated_action(app, tool)

        return app

    def _bind_gated_action(self, app, tool: _Tool) -> None:
        gate_level = self.policy._actions[tool.name].level.value

        def gated(**args: Any) -> dict:
            return self.call_action(tool.name, **args)

        gated.__name__ = tool.name
        app.tool(
            name=tool.name,
            description=f"[gate={gate_level}] {tool.description}",
        )(gated)

    def run(
        self,
        transport: str = "stdio",
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        """Start the MCP server.

        transport: ``stdio`` (default, local/CLI) or ``streamable-http`` / ``sse``
        for a long-lived networked server (containers, so the agent can reach it).
        host/port apply only to the HTTP transports.
        """
        app = self.to_mcp()
        if transport == "stdio":
            app.run("stdio")
            return
        kwargs: dict[str, Any] = {}
        if host is not None:
            kwargs["host"] = host
        if port is not None:
            kwargs["port"] = port
        app.run(transport, **kwargs)

