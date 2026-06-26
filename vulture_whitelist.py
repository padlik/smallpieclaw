# vulture whitelist — items that are legitimately "unused" from vulture's perspective
# Run: python -m vulture *.py vulture_whitelist.py --min-confidence 60
#
# Each entry uses a dummy reference so vulture sees the symbol as "used".

from main import _NightlyRotatingFileHandler
from memory_store import ShortTermMemory
from sub_agent_registry import SubAgentRegistry
from skill_registry import Skill
from interfaces import LLMProvider, ToolBackend, MemoryBackend, NotifyFn  # noqa: E402
from interfaces import ToolRegistryProtocol, MCPManagerProtocol  # noqa: E402

# _NightlyRotatingFileHandler: overrides logging.handlers.TimedRotatingFileHandler
# — doRollover and rolloverAt are called by the Python logging framework internals,
#   not by application code directly.
_NightlyRotatingFileHandler.doRollover
_NightlyRotatingFileHandler.rolloverAt

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
ToolRegistryProtocol.refresh
MCPManagerProtocol.has_tool
MCPManagerProtocol.call_tool
MCPManagerProtocol.list_servers
MCPManagerProtocol.get_tools

# config_schema.py — public API classes and functions
from config_schema import parse_config, AppConfig, TelegramConfig, AgentConfig  # noqa: E402
from config_schema import ModelConfig, EmbeddingsConfig, SchedulerConfig  # noqa: E402
from config_schema import PathsConfig, MCPServerConfig  # noqa: E402
parse_config
AppConfig
TelegramConfig
AgentConfig
ModelConfig
EmbeddingsConfig
SchedulerConfig
PathsConfig
MCPServerConfig

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
