"""Value shapes shared by several methods and events (declared once, imported everywhere)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .base import JsonValue, Params, Payload, Result, WireEnum


class OpenModel(Result):
    """A result/payload row whose known fields are typed but which the producer may extend
    (the closed set is owned elsewhere: hermes_state rows, provider inventories)."""

    model_config = Result.model_config | {"extra": "allow"}


class AccountUsageWindow(Result):
    """One provider quota window (``tui_gateway/server.py::_account_usage_wire``)."""

    period: str
    used_percent: float
    reset_at: str | None = None


class AccountUsage(Result):
    """The active provider's quota snapshot as the TUI capacity row renders it."""

    provider: str
    fetched_at: str
    # Always present when the snapshot is: the wire helper returns None instead of an
    # empty window list, so the field is required rather than defaulted.
    windows: list[AccountUsageWindow]


class AccountUsageGroupWindow(Result):
    period: str
    used_percent: float
    reset_at: str | None = None
    rolled: bool | None = None
    # The provider's own window marker (e.g. opencode-go's "rate-limited"); outranks used_percent
    # when deciding a window is spent (spec D2/D25). Absent on the legacy status-group windows.
    status: str | None = None
    window_seconds: int | None = None


class AccountUsageError(Result):
    status: int
    message: str = ""


class AccountUsageStates(Result):
    """Per-class account counts for a ``quota_aware`` provider's per-profile view (D14/D41)."""

    covered: int = 0
    overflow: int = 0
    exhausted: int = 0


class AccountUsageAccount(Result):
    """One credential in a ``quota_aware`` provider's per-profile view (``agent/quota_state.py::read_view``).

    ``state`` (covered/overflow/exhausted, spec D1) and ``read_health`` (ok/stale/error, D26) are
    separate: an unreadable account keeps its effective state and is never ``unknown`` merely
    because a read failed. Windows are this account's own - never a sum or an average (D14/D34).
    """

    fingerprint: str
    label: str | None = None
    entry_id: str | None = None
    state: str
    metered: bool = False
    read_health: str
    windows: list[AccountUsageGroupWindow]
    fetched_at: str | None = None
    age_s: int | None = None
    attempted_at: str | None = None
    last_error: str | None = None
    # The failed read's HTTP status (429 rotates), surfaced so the UI can mark an account whose
    # last read was rate-limited without parsing ``last_error``.
    error_status: int | None = None
    backoff_until: str | None = None
    recovery_at: str | None = None


class AccountUsageGroup(Result):
    """One subscription's quota from the shared cross-process state (``agent/quota_state.py``).

    A ``quota_aware`` provider's group is built from its per-profile view (D41): ``windows`` is the
    serving account's own windows and ``accounts[]`` carries every credential's own windows, state
    and ``read_health`` - no blended number. ``serving`` is the fingerprint that last served (D34);
    it is a fact, never a per-session "active" flag (that lives in ``Usage.account_usage_active_account``).
    """

    provider: str
    fetched_at: str | None = None
    age_s: int | None = None
    error: AccountUsageError | None = None
    backoff_until: str | None = None
    windows: list[AccountUsageGroupWindow]
    hint: str | None = None
    multi_account: bool | None = None
    # Live account count for a multi-account pool with no per-account view (legacy snapshot path);
    # None when the credential store cannot name the pool's accounts. Lets the footer name the pool
    # (`·2 accounts`) instead of collapsing it to a literal `err`.
    multi_account_count: int | None = None
    accounts: list[AccountUsageAccount] | None = None
    serving: str | None = None
    serving_since: str | None = None
    states: AccountUsageStates | None = None
    strategy: str | None = None
class PlanGauge(Result):
    """``gateway/session_usage.py::plan_gauge``: a window plan's allowance for the status bar."""

    id: str
    label: str
    used_bp: int
    resets_at_ms: int | None = None


class Usage(OpenModel):
    """``tui_gateway/server.py::_get_usage`` + ``agent/context_breakdown.py::context_usage_fields``."""

    model: str = ""
    input: int = 0
    output: int = 0
    reasoning: int = 0
    prompt: int = 0
    completion: int = 0
    total: int = 0
    calls: int = 0
    compressions: int | None = None
    context_used: int | None = None
    context_max: int | None = None
    context_percent: int | None = None
    context_source: str | None = None
    context_estimated: bool | None = None
    cache_hit_pct: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    avg_latency_s: float | None = None
    avg_tps: float | None = None
    active_subagents: int | None = None
    dev_credits_spent_micros: int | None = None
    cost_usd: float | None = None
    cost_status: str | None = None
    account_usage: AccountUsage | None = None
    account_usage_all: list[AccountUsageGroup] | None = None
    account_usage_active: str | None = None
    # The fingerprint of the credential THIS session is on (D19/D29) - the session-local marker the
    # shared per-account view does not carry, so two sessions on different accounts do not fight
    # over one flag. Falls back to the group's ``serving`` when unresolved.
    account_usage_active_account: str | None = None
    plan_gauge: PlanGauge | None = None


class ProjectRef(Result):
    """``tui_gateway/server.py::_project_info_for_cwd``."""

    id: str
    slug: str
    name: str
    primary_path: str | None = None


class McpServerStatus(OpenModel):
    name: str = ""
    status: str | None = None
    tool_count: int | None = None
    error: str | None = None


class SessionLiveInfo(OpenModel):
    """``tui_gateway/server.py::_session_info`` — the ``session.info`` event and the ``info`` field of
    ``session.create`` / ``session.resume`` / ``session.activate`` results."""

    model: str = ""
    provider: str = ""
    reasoning_effort: str = ""
    # The level the route's entry clamp actually sends for ``reasoning_effort`` ("" when unset/none;
    # equal when verbatim). Lets clients label a clamped Hermes step ("ultra sends max on this route").
    reasoning_effort_wire: str = ""
    service_tier: str = ""
    fast: bool = False
    yolo: bool = False
    approval_mode: str = "manual"
    tools: dict[str, list[str]] = Field(default_factory=dict)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    cwd: str = ""
    branch: str | None = None
    project: ProjectRef | None = None
    terminal_backend: str = ""
    personality: str = ""
    running: bool = False
    turn_started_at: float | None = None
    title: str = ""
    stored_session_id: str = ""
    desktop_contract: int | str | None = None
    version: str = ""
    release_date: str = ""
    update_behind: JsonValue | None = None
    update_command: str = ""
    usage: Usage | None = None
    profile_name: str | None = None
    mcp_servers: list[McpServerStatus] = Field(default_factory=list)
    system_prompt: str | None = None
    credential_warning: str | None = None
    lazy: bool | None = None


class StoredSessionRow(OpenModel):
    """One ``sessions`` row as ``hermes_state`` lists it (``session.list`` / ``session.info`` rows /
    ``sessions.changed``)."""

    id: str
    title: str | None = None
    preview: str | None = None
    source: str | None = None
    model: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    last_active: float | None = None
    message_count: int = 0
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    is_active: bool = False
    cwd: str | None = None
    git_branch: str | None = None
    git_repo_root: str | None = None
    parent_session_id: str | None = None
    pinned: bool | None = None
    unread: bool | None = None
    archived: bool | None = None
    actual_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    handoff_platform: str | None = None
    handoff_state: str | None = None
    lineage_root_id: str | None = Field(default=None, alias="_lineage_root_id")
    lineage_ids: list[str] | None = Field(default=None, alias="_lineage_ids")
    # #121148: provenance of a projected continuation tip — 'compression' when the row
    # continues a sealed segment (automatic rotation), None for plain rows and branches.
    continuation_kind: str | None = None


class ToolLabelKind(WireEnum):
    """Which surface one inner call of a bridged ``tool_call`` runs on."""

    connector = "connector"
    mcp = "mcp"
    tool = "tool"


class ToolLabel(Payload):
    """``tools.tool_labels.ToolLabel`` — what one call executed through the tool_search bridge is,
    in words. Clients render ``text`` (or ``app``/``action`` in their own columns) and never parse
    the tool name themselves."""

    kind: ToolLabelKind
    app: str
    action: str
    emoji: str
    text: str
    name: str
    preview: str = ""


class TranscriptMessage(OpenModel):
    """One transcript row as the gateway PROJECTS it for renderers (``session_history._project_history``):
    ``text``, display-only ``timestamp`` / ``display_kind`` / ``display_metadata``, the durable ``row_id``
    rewind targets, and for tool rows raw ``content``, ``tool_call_id``, ``name``, ``context`` and ``args``.
    Assistant detail sidecars (``reasoning``, …) ride as extra keys."""

    role: str
    text: str | None = None
    content: JsonValue | None = None
    tool_call_id: str | None = None
    timestamp: float | None = None
    row_id: int | None = None
    display_kind: str | None = None
    display_metadata: JsonValue | None = None
    name: str | None = None
    context: str | None = None
    args: dict[str, JsonValue] | None = None
    labels: list[ToolLabel] | None = None
    reasoning: str | None = None


class SubagentStatus(WireEnum):
    """Lifecycle of one delegated child (``tools/delegate_tool_child_run.py``); ``failed`` /
    ``error`` / ``timeout`` / ``interrupted`` / ``completed`` are terminal."""

    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    error = "error"
    timeout = "timeout"
    interrupted = "interrupted"


TERMINAL_SUBAGENT_STATUSES = frozenset({
    SubagentStatus.completed, SubagentStatus.failed, SubagentStatus.error,
    SubagentStatus.timeout, SubagentStatus.interrupted,
})


class PendingApproval(OpenModel):
    """One unresolved ``tools/approval.py`` gateway queue entry as ``server._approval_request_payload``
    renders it (command redacted; ``choices`` precomputed). The key set is owned by the approval tool."""

    request_id: str | None = None
    command: str | None = None
    description: str | None = None
    pattern_key: str | None = None
    pattern_keys: list[str] | None = None
    allow_permanent: bool | None = None
    allow_session: bool | None = None
    smart_denied: bool | None = None
    choices: list[str] | None = None
    tool_name: str | None = None


class MessageReaction(OpenModel):
    """One persisted reaction row (``hermes_state_messages.set_message_reaction``); ``seen`` is
    stamped once announced."""

    emoji: str
    author: str
    at: float | None = None
    seen: bool | None = None


class SessionParams(Params):
    """Any method addressed at one live session."""

    session_id: str
    profile: str | None = None


class ProfileParams(Params):
    """Any method the desktop may route to a named profile (``requestGatewayForProfile`` adds ``profile``)."""

    profile: str | None = None


class SessionOwner(Params):
    type: Literal["session"]
    session_id: str = Field(min_length=1)


class AccountOwner(Params):
    type: Literal["account"]


ConnectorOwner = Annotated[SessionOwner | AccountOwner, Field(discriminator="type")]


class OkResult(Result):
    ok: bool = True


class StatusResult(OpenModel):
    status: str


class EmptyResult(Result):
    pass


class EmptyPayload(Payload):
    pass


class PlanUsageGauge(Result):
    """One server allowance on ``ModelOptionProvider.plan_usage``, in the server's order. ``label`` is the
    server's id (``day`` / ``week`` / ``month`` / ``top_tier`` have client words; any other id shows
    ``title``). ``scope``: which models a full one stops (``all`` / ``top_tier``)."""

    label: str
    left_fraction: float
    style: str
    resets_at_ms: int | None = None
    scope: str = "all"
    title: str = ""


class PlanUsage(Result):
    """``hermes_cli/model_picker_usage.py::PickerUsage.to_payload`` — usage-window plan chrome."""

    kind: str
    plan: str | None = None
    gauges: list[PlanUsageGauge] = Field(default_factory=list)
    # Ids the gateway labels ``top_tier`` (Large).
    top_tier_model_ids: list[str] = Field(default_factory=list)
    # Ids billed to a subscription the account holds: ``free`` in ``usage_levels`` (never dimmed), no "Free" tag.
    subscription_model_ids: list[str] = Field(default_factory=list)
    featured_model_ids: list[str] = Field(default_factory=list)
    paused_reason: str | None = None
    # Past a full allowance, top-up credits pay (pay as you go): Keep going on credits on + a balance.
    # False: the turn stops. None: unknown (older portal).
    credits_pay: bool | None = None


__all__ = [
    "TERMINAL_SUBAGENT_STATUSES", "AccountOwner", "AccountUsage", "AccountUsageAccount",
    "AccountUsageError", "AccountUsageGroup", "AccountUsageGroupWindow", "AccountUsageStates",
    "AccountUsageWindow", "ConnectorOwner",
    "EmptyPayload", "EmptyResult", "McpServerStatus",
    "MessageReaction", "OkResult", "OpenModel", "PendingApproval",
    "PlanGauge", "PlanUsage", "PlanUsageGauge",
    "ProfileParams", "ProjectRef", "SessionLiveInfo", "SessionOwner", "SessionParams", "StatusResult", "StoredSessionRow",
    "SubagentStatus", "ToolLabel", "ToolLabelKind", "TranscriptMessage", "Usage",
]
