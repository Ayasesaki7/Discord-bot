"""Agent runtime foundations for the Discord chat integration."""

from .privacy import (
    AgentRequestContext,
    ConversationScope,
    CrossTenantAccessError,
    PrivacyBoundary,
    ReplyBinding,
    ScopedSessionIdentity,
    build_safe_debug_snapshot,
    build_sensitive_content_metadata,
    sensitive_content_logging_enabled,
)
from .dsh_runtime import (
    DshJsonRpcProcess,
    DshRuntimeError,
    DshRuntimeTemplate,
    DshTenantRuntimePool,
    DshTurnResult,
)
from .tool_server import AgentToolServer, AgentToolServerError
from .project_tools import ProjectToolError, ProjectToolHost
from .plugin_manager import PluginManager, PluginManagerError
from .credentials import CredentialUpdateError, ServiceCredentialStore
from .code_settings import (
    AgentCodeSettings,
    AgentCodeSettingsError,
    AgentCodeSettingsStore,
    WebSearchSettingsStore,
)

__all__ = [
    "AgentRequestContext",
    "ConversationScope",
    "CrossTenantAccessError",
    "PrivacyBoundary",
    "ReplyBinding",
    "ScopedSessionIdentity",
    "build_safe_debug_snapshot",
    "build_sensitive_content_metadata",
    "sensitive_content_logging_enabled",
    "DshJsonRpcProcess",
    "DshRuntimeError",
    "DshRuntimeTemplate",
    "DshTenantRuntimePool",
    "DshTurnResult",
    "AgentToolServer",
    "AgentToolServerError",
    "ProjectToolError",
    "ProjectToolHost",
    "PluginManager",
    "PluginManagerError",
    "CredentialUpdateError",
    "ServiceCredentialStore",
    "AgentCodeSettings",
    "AgentCodeSettingsError",
    "AgentCodeSettingsStore",
    "WebSearchSettingsStore",
]
