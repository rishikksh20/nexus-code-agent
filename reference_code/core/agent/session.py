import json
from datetime import datetime
import uuid
from typing import Any

from core.context.loop_detector import LoopDetector
from core.context.compaction import ChatCompactor
from core.config.loader import get_data_dir
from core.client.llm_client import LLMClient
from core.config.config import Config
from core.context.manager import ContextManager
from core.hooks.hook_system import HookSystem
from core.safety.approval import ApprovalManager
from core.tools.discovery import ToolDiscoveryManager
from core.tools.registry import create_default_registry
from core.tools.mcp.mcp_manager import MCPManager



class Session:
    def __init__(self, config: Config
                 ):
        self.config = config
        self.client: LLMClient = LLMClient(config)
        self.tool_registry = create_default_registry(config)
        self.mcp_manager: MCPManager = MCPManager(config)
        self.context_manager: ContextManager|None = None
        self.discovery_manager = ToolDiscoveryManager(
            self.config,
            self.tool_registry,
        )
        self.approval_manager = ApprovalManager(config.approval, self.config.cwd)
        self.loop_detector = LoopDetector()
        self.chat_compactor = ChatCompactor(self.client)
        self.hook_system = HookSystem(config)
        self.session_id = str(uuid.uuid4())
        self.created_at = datetime.now()
        self.updated_at = datetime.now()


        self.turn_count = 0


    async def initialize(self) -> None:
        await self.mcp_manager.initialize()

        self.mcp_manager.register_tools(self.tool_registry)
        self.discovery_manager.discover_all()
        self.context_manager = ContextManager(self.config, user_memory=self._load_memory(), tools=self.tool_registry.get_tools())

    def _load_memory(self) -> str | None:
        data_dir = get_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / "user_memory.json"

        if not path.exists():
            return None

        try:
            content = path.read_text(encoding="utf-8")
            data =  json.loads(content)
            entries = data["entries"]
            if not entries:
                return None

            lines = ["User preferences and notes :"]
            for key, entry in entries.items():
                lines.append("- {}: {}".format(key, entry))
            return "\n".join(lines)
        except Exception:
            return None

    def increment_turn(self) -> int:
        self.turn_count += 1
        self.updated_at = datetime.now()
        return self.turn_count

    def get_stats(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "turn_count": self.turn_count,
            "message_count": self.context_manager.message_count,
            "token_usage": self.context_manager.total_usage,
            "tools_count": len(self.tool_registry.get_tools()),
            "mcp_servers": len(self.tool_registry.connected_mcp_servers),
        }