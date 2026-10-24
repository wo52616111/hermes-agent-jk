"""Per-server rows of an installed portable plugin for ``plugins.manage list`` (``methods_tools._plugin_rows``).

Bodies are rebound onto server.py's globals (method_ctx.bind_module) and reference them bare.
"""

from __future__ import annotations

from pathlib import Path

from .method_ctx import bind_module


def _plugin_server_rows(
    plugin_dir: Path | None, key: str, *, portable: bool,
    catalog_titles: dict[str, str] | None = None,
) -> list[dict]:
    if not portable or plugin_dir is None:
        return []
    package = _tools_mod("hermes_cli.agent_plugins").load_agent_plugin(plugin_dir, plugin_dir)
    namespace = package.manifest.get("extensions", {}).get("com.nousresearch.hermes", {})
    declared = namespace.get("servers", {})
    if not isinstance(declared, dict):
        return []
    server_name_for = _tools_mod("hermes_cli.plugins_manifest").portable_mcp_server_name
    liveness = _tools_mod("tools.mcp_liveness")
    core = _tools_mod("tools.mcp_tool_common")._core
    resolve_key = _tools_mod("tools.mcp_tool_scope")._resolve_server_key
    # The server sentence's app name: the curated catalog title when the package is a catalog
    # install, else the manifest name, else the server slug the declaration carries — a raw
    # slug reads like an error code (#119975). *catalog_titles* is pre-resolved by the caller
    # (one live-catalog resolution per listing): a per-plugin ``get_live_catalog_entry`` would
    # re-resolve the whole catalog once per installed plugin.
    display_name = str(package.manifest.get("name") or "") or None
    sidecar = _tools_mod("hermes_cli.plugins_cmd_catalog").catalog_install_record(plugin_dir)
    if sidecar:
        title = (catalog_titles or {}).get(str(sidecar.get("catalog_name") or ""))
        if title:
            display_name = title
    rows = []
    for name in sorted(declared):
        internal_name = server_name_for(key, name)
        connection_key = resolve_key(internal_name)
        server = core._servers.get(connection_key)
        connected = server is not None and (server.session is not None or server._is_recycled_stdio())
        if connected:
            rows.append({"name": name, "state": "connected", "sentence": ""})
            continue
        decl = _tools_mod("hermes_platform.declaration").lookup(internal_name)
        status = liveness.status(internal_name)
        if decl is None or status is None:
            rows.append({"name": name, "state": "unknown", "sentence": ""})
            continue
        rows.append({
            "name": name,
            "state": status.state,
            "sentence": liveness.describe(decl, status.availability, status.state, display_name),
        })
    return rows


def _plugin_tool_ownership() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Which tools each loaded plugin's OWN code produces, by manifest key: ``(tools, prefixes)``.

    Desktop lets a plugin's desktop half render media only from these tools, so the answer comes
    from the loader's records, never from a manifest claim. Native: names the plugin registered,
    minus built-in tool names (an approved override of ``text_to_speech`` is still the core voice
    reply to the user) and minus any name two plugins list. Portable: one ``mcp__<server>__``
    prefix per MCP server the loader attributed to the package (duplicate server names are refused
    at load, and a clamped long tool name keeps its prefix)."""
    plugins_mod = _tools_mod("hermes_cli.plugins")
    toolsets = _tools_mod("toolsets")
    schema = _tools_mod("tools.mcp_tool_schema")
    plugins_mod.discover_plugins()
    manager = plugins_mod.get_plugin_manager()
    builtin = set(toolsets._HERMES_CORE_TOOLS)
    for entry in toolsets.TOOLSETS.values():
        builtin.update(entry.get("tools", ()))
    listed = {key: set(loaded.tools_registered) - builtin for key, loaded in manager._plugins.items()}
    seen: dict[str, int] = {}
    for names in listed.values():
        for name in names:
            seen[name] = seen.get(name, 0) + 1
    tools = {key: sorted(n for n in names if seen[n] == 1) for key, names in listed.items()}
    prefixes: dict[str, list[str]] = {}
    for server, owner in manager.get_portable_mcp_server_plugins().items():
        prefix = f"{schema.MCP_TOOL_NAME_PREFIX}{schema.sanitize_mcp_name_component(server)}__"
        prefixes.setdefault(owner, []).append(prefix)
    return tools, {key: sorted(values) for key, values in prefixes.items()}


def register(server) -> None:
    bind_module(globals(), server)
