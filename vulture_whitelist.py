# vulture whitelist — items that are legitimately "unused" from vulture's perspective
# Run: python -m vulture *.py vulture_whitelist.py --min-confidence 60
#
# Each entry uses a dummy reference so vulture sees the symbol as "used".

from memory_store import ShortTermMemory
from sub_agent_registry import SubAgentRegistry
from skill_registry import Skill
from interfaces import LLMProvider, ToolBackend, MemoryBackend, NotifyFn  # noqa: E402
from interfaces import ToolRegistryProtocol, MCPManagerProtocol  # noqa: E402
from interfaces import ToolCall, ChatResponse, ProviderContext  # noqa: E402

# ShortTermMemory.get_messages: public API — may be used by external callers or tests
ShortTermMemory.get_messages

# SubAgentRegistry.find_by_label: public API for looking up agents by label
SubAgentRegistry.find_by_label

# Skill dataclass fields: populated from YAML/SKILL.md metadata; not always accessed
# programmatically but are part of the public data model
Skill.license
Skill.compatibility
Skill.metadata

# Protocol interface methods — abstract contracts, not called directly
LLMProvider.chat
LLMProvider.chat_with_fallback
LLMProvider.chat_with_tools
LLMProvider.chat_with_tools_fallback
LLMProvider.embed
ToolBackend.execute
MemoryBackend.get
MemoryBackend.set
MemoryBackend.delete
MemoryBackend.search
NotifyFn.__call__
parse_mode = None
parse_mode
ToolRegistryProtocol.list_tools
ToolRegistryProtocol.get_tool
MCPManagerProtocol.has_tool
MCPManagerProtocol.call_tool
MCPManagerProtocol.list_servers
MCPManagerProtocol.get_tools

# config_schema.py — public API classes and functions
from config_schema import parse_config, AppConfig, TelegramConfig, AgentConfig  # noqa: E402
from config_schema import ModelConfig, EmbeddingsConfig, SchedulerConfig  # noqa: E402
from config_schema import PathsConfig, MCPServerConfig, OAuthConfig  # noqa: E402
parse_config
AppConfig
TelegramConfig
AgentConfig
ModelConfig
EmbeddingsConfig
SchedulerConfig
PathsConfig
MCPServerConfig
OAuthConfig

# builtin_tools/access_control.py — public API
from builtin_tools.access_control import (  # noqa: E402
    ZoneClassification,
    TrustedDir,
    TrustedZoneChecker,
    GrantTracker,
)
ZoneClassification.TRUSTED
ZoneClassification.REQUEST_GRANT
ZoneClassification.UNRECOGNISED
TrustedDir.path
TrustedDir.added
TrustedZoneChecker.classify
TrustedZoneChecker.add_trusted
TrustedZoneChecker.remove_trusted
TrustedZoneChecker.list_user_trusted
GrantTracker
GrantTracker.add
GrantTracker.reset
GrantTracker.snapshot

# mcp_oauth.py — public API for MCP OAuth 2.0 support
from mcp_oauth import FileTokenStorage, CallbackServer, OAuthProviderFactory  # noqa: E402
FileTokenStorage
CallbackServer
OAuthProviderFactory

# xdg.py — XDGPaths field exposed for MCP token storage
from xdg import XDGPaths  # noqa: E402
XDGPaths
XDGPaths.mcp_tokens_dir

# agent_logging.py — structlog logging backbone public API
from agent_logging import (  # noqa: E402
    LogEvent,
    setup_bootstrap,
    setup_logging,
    bind_run_context,
    reset_run_context,
    clear_run_context,
    get_logger,
    log_event,
)
LogEvent
setup_bootstrap
setup_logging
bind_run_context
reset_run_context
clear_run_context
get_logger
log_event

# graph_memory.py — backfill public API
from graph_memory import (  # noqa: E402
    BackfillEntryResult,
    BackfillResult,
    backfill_longterm_to_graph,
    build_extraction_llm_call,
)
BackfillEntryResult
BackfillResult
backfill_longterm_to_graph
build_extraction_llm_call
backfill_longterm_to_graph

# LongTermMemory.entries — safe snapshot API used by backfill CLI
from memory_store import LongTermMemory  # noqa: E402
LongTermMemory.entries

# strategy_memory.py — public API classes
from strategy_memory import StrategyMemory, Strategy  # noqa: E402
StrategyMemory.add
StrategyMemory.get
StrategyMemory.decay_all
StrategyMemory.archive_low_confidence
StrategyMemory.get_top_k
StrategyMemory.save
StrategyMemory.load
Strategy.to_dict
Strategy.from_dict

# error_registry.py — public API
from error_registry import ErrorTypeInfo, ErrorTypeRegistry  # noqa: E402
ErrorTypeInfo.recoverable
ErrorTypeInfo.max_retries
ErrorTypeInfo.backoff_base
ErrorTypeInfo.requires_complex_recovery
ErrorTypeRegistry.register
ErrorTypeRegistry.get
ErrorTypeRegistry.register_defaults

# tests/test_graph_memory_backfill.py — pytest false positives
# 'p' is a required lambda parameter matching the llm_call_fn protocol (str -> str)
# 'error_llm' is a pytest fixture injected by name but not referenced directly in the
# test body (it overrides mock_store's side effect via the fixture mechanism)
from tests.test_graph_memory_backfill import empty_llm, error_llm  # noqa: E402
empty_llm
error_llm

# builtin_tools/schemas.py — public API for native tool calling
from builtin_tools.schemas import build_tool_definitions, BUILTIN_TOOL_SCHEMAS, PSEUDO_TOOL_SCHEMAS  # noqa: E402
build_tool_definitions
BUILTIN_TOOL_SCHEMAS
PSEUDO_TOOL_SCHEMAS

# interfaces.py — native tool calling types
ToolCall
ChatResponse

# interfaces.py — provider backend dependency bag (imported by llm_client for Phase 2)
ProviderContext

# config_schema.py — PathsConfig.workspace_dir field
from config_schema import PathsConfig as _PathsConfig  # noqa: E402, F811
_PathsConfig.workspace_dir

# conversation_io.py — public helper API for conversation persistence
from conversation_io import (  # noqa: E402
    _load_or_create_conversation_id,
    _save_conversation,
)
_load_or_create_conversation_id
_save_conversation

# main.py — public helper API used across modules and tests
from main import (  # noqa: E402
    _load_conversation,
    _cleanup_old_session_logs,
)
_load_conversation
_cleanup_old_session_logs

# mcp_client.py — public API for MCP token status display
from mcp_client import MCPManager  # noqa: E402
_mcp_manager_type = MCPManager
_mcp_manager_type.get_token_info  # type: ignore[attr-defined]

# builtin_executor.py — public attributes set/wired externally
from builtin_executor import BuiltinExecutor  # noqa: E402
_builtin_executor_type = BuiltinExecutor
_builtin_executor_type.conversation_id  # type: ignore[attr-defined]
_builtin_executor_type._agent_name  # type: ignore[attr-defined]
_builtin_executor_type._vault_secrets  # type: ignore[attr-defined]
_builtin_executor_type._vault_secrets  # type: ignore[attr-defined]

# config_schema.py — AgentConfig new retention field
from config_schema import AgentConfig as _AgentConfig  # noqa: E402, F811
_AgentConfig.session_logs_retention_days


