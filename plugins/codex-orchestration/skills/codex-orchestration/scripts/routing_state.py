#!/usr/bin/env python3
"""Fail-closed validation for persisted Codex-Orchestration routing state.

This module deliberately depends only on the Python standard library so every
packaged entry point can import the same contract validator.
"""

from __future__ import annotations

import re
from typing import Any


MANAGED_MARKER = "[codex-orchestration managed-policy v1]"
ROUTING_TOOL_NAMESPACE = "agents"
FABLE_MODEL = "claude-fable-5"
FABLE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
FABLE_SERVERS = frozenset(
    {
        "fable-advisor-python3",
        "fable-advisor-python",
        "fable-advisor-py",
    }
)
FABLE_AUTH_MODES = frozenset({"subscription", "api", "auto"})
FABLE_API_SOURCES = frozenset({"config-file", "environment", "user-settings"})
FABLE_TRANSPORTS = frozenset({"claude-code", "direct-api"})
FABLE_ADVISOR_PATHS = frozenset({"claude-code-cli", "ccswitch", "python-api"})

_SCHEMA_POLICY_PAIRS = frozenset({(1, 1), (2, 2), (3, 2), (3, 3), (4, 4)})
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,199}$")
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_EFFORT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_BASE_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "policy_version",
        "managed_by",
        "config_file",
        "executor",
        "advisor",
        "managed",
        "previous",
        "scalar_origin",
        "managed_feature",
    }
)
_BASE_MANAGED_KEYS = frozenset({"mode", "usage", "metadata", "namespace"})
_BASE_PREVIOUS_KEYS = frozenset({"mode", "usage", "metadata", "namespace"})


class RoutingStateError(ValueError):
    """The persisted value is not one exact supported routing-state contract."""


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise RoutingStateError(detail)


def _has_marker_first_line(value: Any) -> bool:
    if type(value) is not str:
        return False
    first_line, separator, body = value.partition("\n")
    return first_line == MANAGED_MARKER and separator == "\n" and bool(body.strip())


def _validate_snapshot(value: Any, expected_type: type) -> None:
    _require(type(value) is dict, "restore snapshot must be an object")
    known = value.get("known")
    present = value.get("present")
    _require(type(known) is bool, "snapshot known must be boolean")
    _require(type(present) is bool, "snapshot present must be boolean")

    if not known:
        _require(
            not present and set(value) == {"known", "present"},
            "unknown snapshot must be exactly absent",
        )
    elif not present:
        _require(
            set(value) == {"known", "present"},
            "absent snapshot has unexpected fields",
        )
    else:
        _require(
            set(value) == {"known", "present", "value"},
            "present snapshot has the wrong shape",
        )
        _require(
            type(value["value"]) is expected_type,
            "present snapshot has the wrong value type",
        )


def _fable_advisor_path(transport: str, api_source: str | None) -> str:
    if transport == "claude-code":
        return "claude-code-cli"
    if transport == "direct-api" and api_source == "user-settings":
        return "ccswitch"
    if transport == "direct-api" and api_source in {"config-file", "environment"}:
        return "python-api"
    raise RoutingStateError("Fable advisor path is invalid")


def _validate_route(
    route: Any, *, seat: str, schema: int, policy_version: int
) -> str:
    _require(type(route) is dict, f"{seat} route must be an object")
    kind = route.get("kind")
    _require(type(kind) is str, f"{seat} route kind must be a string")

    if kind == "model":
        _require(
            set(route) == {"kind", "model", "effort"},
            f"{seat} model route has the wrong shape",
        )
        _require(
            type(route["model"]) is str and _MODEL_RE.fullmatch(route["model"]) is not None,
            f"{seat} model route has an invalid model",
        )
        _require(
            type(route["effort"]) is str
            and _EFFORT_RE.fullmatch(route["effort"]) is not None,
            f"{seat} model route has an invalid effort",
        )
    elif kind == "agent":
        _require(
            set(route) == {"kind", "agent"},
            f"{seat} agent route has the wrong shape",
        )
        _require(
            type(route["agent"]) is str and _AGENT_RE.fullmatch(route["agent"]) is not None,
            f"{seat} agent route has an invalid name",
        )
    elif kind == "fable":
        _require(
            seat in {"planner", "advisor"}
            and schema >= 2
            and not (seat == "planner" and (schema, policy_version) != (3, 3) and schema != 4),
            f"{seat} cannot use Fable in schema {schema}",
        )
        base_keys = {"kind", "model", "effort", "server"}
        supports_direct_metadata = seat == "advisor" and (
            (schema, policy_version) == (3, 2) or schema == 4
        )
        expected_keys = set(base_keys)
        if supports_direct_metadata:
            expected_keys.update({"auth_mode", "transport", "path"})
            if route.get("auth_mode") == "api":
                expected_keys.add("api_source")
        _require(
            set(route) == expected_keys,
            f"{seat} Fable route has the wrong shape",
        )
        _require(route["model"] == FABLE_MODEL, "Fable model is not pinned")
        _require(
            type(route["effort"]) is str and route["effort"] in FABLE_EFFORTS,
            "Fable effort is unsupported",
        )
        _require(
            type(route["server"]) is str and route["server"] in FABLE_SERVERS,
            "Fable server is unsupported",
        )
        if supports_direct_metadata:
            auth_mode = route["auth_mode"]
            transport = route["transport"]
            api_source = route.get("api_source")
            _require(
                type(auth_mode) is str and auth_mode in FABLE_AUTH_MODES,
                "Fable auth mode is unsupported",
            )
            _require(
                type(transport) is str and transport in FABLE_TRANSPORTS,
                "Fable transport is unsupported",
            )
            _require(
                (
                    auth_mode == "api"
                    and type(api_source) is str
                    and api_source in FABLE_API_SOURCES
                )
                or (auth_mode != "api" and api_source is None),
                "Fable API source does not match authentication mode",
            )
            _require(
                transport != "direct-api" or auth_mode == "api",
                "Fable direct API requires API authentication",
            )
            _require(
                api_source != "config-file" or transport == "direct-api",
                "Fable config-file source requires direct API",
            )
            _require(
                type(route["path"]) is str
                and route["path"] in FABLE_ADVISOR_PATHS
                and route["path"] == _fable_advisor_path(transport, api_source),
                "Fable advisor path does not match its transport and source",
            )
    else:
        raise RoutingStateError(f"{seat} route kind is unsupported")
    return kind


def _validate_route_separation(planner: Any, advisor: Any) -> None:
    if planner is None or advisor is None:
        return
    planner_kind = planner["kind"]
    advisor_kind = advisor["kind"]
    same_route = (
        planner_kind == advisor_kind == "model"
        and planner["model"] == advisor["model"]
    ) or (
        planner_kind == advisor_kind == "agent"
        and planner["agent"] == advisor["agent"]
    ) or planner_kind == advisor_kind == "fable"
    _require(not same_route, "Planner and Advisor routes are not independent")


def _validate_scalar_conversion(
    state: dict[str, Any], managed: dict[str, Any], *, explicit_v2: bool
) -> None:
    scalar_origin = state["scalar_origin"]
    managed_feature = state["managed_feature"]
    if scalar_origin is None:
        _require(managed_feature is None, "null scalar origin requires null managed feature")
        return

    _require(type(scalar_origin) is bool, "scalar origin must be null or boolean")
    _require(type(managed_feature) is dict, "scalar conversion must save a table")
    expected_feature = {
        "enabled",
        "hide_spawn_agent_metadata",
        "tool_namespace",
        "multi_agent_mode_hint_text",
        "usage_hint_text",
    }
    if "v2_thread_limit" in managed:
        expected_feature.add("max_concurrent_threads_per_session")
    _require(set(managed_feature) == expected_feature, "managed scalar conversion table has the wrong shape")
    _require(
        type(managed_feature["enabled"]) is bool
        and managed_feature["enabled"] is (True if explicit_v2 else scalar_origin),
        "managed scalar conversion enabled value is forged",
    )
    _require(
        type(managed_feature["hide_spawn_agent_metadata"]) is bool
        and managed_feature["hide_spawn_agent_metadata"] is False,
        "managed scalar conversion metadata value is forged",
    )
    _require(
        type(managed_feature["tool_namespace"]) is str
        and managed_feature["tool_namespace"] == ROUTING_TOOL_NAMESPACE,
        "managed scalar conversion namespace is forged",
    )
    _require(
        type(managed_feature["multi_agent_mode_hint_text"]) is str
        and managed_feature["multi_agent_mode_hint_text"] == managed["mode"],
        "managed scalar conversion mode is forged",
    )
    _require(
        type(managed_feature["usage_hint_text"]) is str
        and managed_feature["usage_hint_text"] == managed["usage"],
        "managed scalar conversion usage is forged",
    )
    if "v2_thread_limit" in managed:
        _require(
            managed_feature["max_concurrent_threads_per_session"]
            == managed["v2_thread_limit"],
            "managed scalar conversion thread limit is forged",
        )


def validate_routing_state(value: Any) -> dict[str, Any]:
    """Validate and return one exact, complete persisted supported schema.

    Unknown keys and future extensions are rejected intentionally. Callers must
    perform their own secure file read and any caller-specific path/seat checks.
    """

    _require(type(value) is dict, "routing state must be an object")
    schema = value.get("schema")
    policy_version = value.get("policy_version")
    _require(
        type(schema) is int and any(schema == pair[0] for pair in _SCHEMA_POLICY_PAIRS),
        "schema must be an exact supported integer",
    )
    _require(
        type(policy_version) is int
        and (schema, policy_version) in _SCHEMA_POLICY_PAIRS,
        "policy version does not match schema",
    )

    expected_top = set(_BASE_TOP_LEVEL_KEYS)
    if (schema, policy_version) in {(3, 3), (4, 4)}:
        expected_top.add("planner")
    _require(set(value) == expected_top, "top-level state shape is unsupported")
    _require(value["managed_by"] == "codex-orchestration", "state owner is invalid")
    _require(
        type(value["config_file"]) is str
        and bool(value["config_file"])
        and "\x00" not in value["config_file"],
        "config path is invalid",
    )

    _validate_route(
        value["executor"], seat="executor", schema=schema, policy_version=policy_version
    )
    planner = value.get("planner")
    advisor = value["advisor"]
    if planner is not None:
        _validate_route(
            planner, seat="planner", schema=schema, policy_version=policy_version
        )
    if advisor is not None:
        _validate_route(
            advisor, seat="advisor", schema=schema, policy_version=policy_version
        )
    _validate_route_separation(planner, advisor)

    managed = value["managed"]
    previous = value["previous"]
    _require(type(managed) is dict, "managed state must be an object")
    _require(type(previous) is dict, "previous state must be an object")
    managed_has_mcp = "mcp" in managed
    previous_has_mcp = "mcp" in previous
    _require(managed_has_mcp == previous_has_mcp, "MCP state and restore data must pair")
    _require(not managed_has_mcp or schema >= 2, "schema 1 cannot contain MCP state")

    expected_managed = set(_BASE_MANAGED_KEYS)
    expected_previous = set(_BASE_PREVIOUS_KEYS)
    explicit_v2 = (schema, policy_version) in {(3, 2), (4, 4)}
    if explicit_v2:
        expected_managed.add("enabled")
        expected_previous.add("enabled")
        _require(managed.get("enabled") is True, "managed v2 activation must be true")
        _validate_snapshot(previous.get("enabled"), bool)
        has_thread_limit = "v2_thread_limit" in managed
        _require(
            has_thread_limit == ("legacy_thread_limit_removed" in managed),
            "managed thread-limit migration fields must pair",
        )
        if has_thread_limit:
            expected_managed.update({"v2_thread_limit", "legacy_thread_limit_removed"})
            expected_previous.update({"v2_thread_limit", "legacy_thread_limit"})
            _require(
                type(managed["v2_thread_limit"]) is int
                and managed["v2_thread_limit"] >= 1,
                "managed v2 thread limit is invalid",
            )
            _require(
                managed["legacy_thread_limit_removed"] is True,
                "legacy thread limit removal marker is invalid",
            )
            _validate_snapshot(previous.get("v2_thread_limit"), int)
            _validate_snapshot(previous.get("legacy_thread_limit"), int)
    if managed_has_mcp:
        expected_managed.add("mcp")
        expected_previous.add("mcp")
    _require(set(managed) == expected_managed, "managed state has the wrong shape")
    _require(set(previous) == expected_previous, "restore state has the wrong shape")
    _require(_has_marker_first_line(managed["mode"]), "managed mode marker is invalid")
    _require(_has_marker_first_line(managed["usage"]), "managed usage marker is invalid")
    _require(managed["metadata"] is False, "managed metadata must be false")
    _require(
        managed["namespace"] == ROUTING_TOOL_NAMESPACE,
        "managed namespace is invalid",
    )

    for key, expected_type in (
        ("mode", str),
        ("usage", str),
        ("metadata", bool),
        ("namespace", str),
    ):
        _validate_snapshot(previous[key], expected_type)

    fable_routes = [
        route
        for route in (planner, advisor)
        if type(route) is dict and route.get("kind") == "fable"
    ]
    _require(len(fable_routes) <= 1, "more than one Fable seat is configured")
    if managed_has_mcp:
        managed_mcp = managed["mcp"]
        previous_mcp = previous["mcp"]
        _require(type(managed_mcp) is dict and bool(managed_mcp), "MCP state is empty")
        _require(type(previous_mcp) is dict, "MCP restore state must be an object")
        _require(
            set(managed_mcp) == set(previous_mcp)
            and set(managed_mcp).issubset(FABLE_SERVERS),
            "MCP state has unsupported or unpaired servers",
        )
        _require(
            all(type(enabled) is bool for enabled in managed_mcp.values()),
            "MCP enabled values must be booleans",
        )
        for saved in previous_mcp.values():
            _validate_snapshot(saved, bool)
        true_servers = [server for server, enabled in managed_mcp.items() if enabled]
    else:
        true_servers = []

    if fable_routes:
        selected_server = fable_routes[0]["server"]
        _require(
            true_servers == [selected_server],
            "MCP state must enable exactly the selected Fable launcher",
        )
    else:
        _require(not true_servers, "MCP state enables a launcher without a Fable seat")

    _validate_scalar_conversion(value, managed, explicit_v2=explicit_v2)
    return value
