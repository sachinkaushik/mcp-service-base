# mcp-service-base — Overview

A small, reusable Python library that lets every Central QSR Agent service (real
pipeline or simulator) expose its data and actions to the agent through **one
uniform contract**. Write a service once, and the agent talks to all services the
same way — it can't tell a real pipeline from a simulator.

## What it does

A service imports this library, declares its **event schema** and its
**read/act tools**, and runs it. The library provides everything else:

| Concern | Provided by |
|---|---|
| MCP server (describe / subscribe / read / act) | `ServiceServer.to_mcp()` |
| Event envelope + emit | `envelope.py`, `emit()` |
| Durable, replayable log (db **or** file) | `SQLiteLog`, `JSONLFileLog` |
| Event delivery / fan-out + retries | `Delivery` (EventHub / Webhook / Off) |
| Action safety (auto / notify / approval / blocked) | `PolicyGate` |
| Metrics / timing | `Telemetry` (pluggable, `NullTelemetry` to opt out) |
| Per-tool timeout | `tool_timeout_s` |

The service writes only **domain code**; the library is domain-agnostic.

## How a service uses it

```python
from mcp_service_base import ServiceServer, ServiceConfig, GateLevel

svc = ServiceServer.from_config(ServiceConfig(
    service="suspicious_activity", store_id="store_001",
    log_backend="jsonl", log_path="/data/sad.jsonl",
))

@svc.read_tool("Get_activity_by_zone")
def get_activity_by_zone(zone: str) -> list[dict]:
    """List activities for a zone."""          # docstring = tool description
    return queries.activity_by_zone(svc.log, zone)

@svc.act_tool("notify_operator", level=GateLevel.AUTOMATIC)
def notify_operator(zone: str, message: str) -> dict:
    """Surface an event to the operator."""
    return {"notified": zone}

svc.run(transport="streamable-http", host="0.0.0.0", port=9000)
```

## How an event flows

```
pipeline → ingest → svc.emit() → durable log (FIRST, idempotent on ref_id)
                                → fan out to the agent (retries)
agent → describe → read tools (direct) / act tools (through Policy Gate)
```

Log-first means nothing is lost and every run is replayable. `ref_id` (the source
message id) makes re-delivery idempotent.

## Design patterns

- **Façade** — apps use `ServiceServer` only, never internal classes.
- **Strategy** — pluggable log (`DurableLog`), delivery (`Sink`), metrics (`MetricsBackend`).
- **Adapter** — `to_mcp()` adapts tools to the MCP SDK (works on 1.x `FastMCP` and 2.x `MCPServer`).

## Versioning

Released as git tags (`v0.1.0` … `v0.2.0`). Each app pins a version, so different
apps can run different versions independently:

```
mcp-service-base[mcp] @ git+https://github.com/sachinkaushik/mcp-service-base.git@v0.2.0
```

## Status

Generic base, proven on the **SLP Suspicious-Activity (SAD)** service (sample data
for now). Hardened as more services are built on it.
