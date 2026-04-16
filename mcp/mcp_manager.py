"""
MCP Manager — 统一管理所有 MCP Server 的工具注册与调用分发。

通过读取 mcp.json 配置，自动实例化对应类型的客户端（SSE / StreamableHttp / Stdio），
并将所有工具以 OpenAI function calling 格式暴露给 MarkiNote AI Agent。

工具命名规范：mcp__<server_name>__<original_tool_name>

使用方式：
    from mcp.mcp_manager import mcp_manager

    tool_defs = mcp_manager.get_tool_definitions()
    result    = mcp_manager.call_tool('mcp__finance-mcp__stock_data', {'code': '600519.SH'})
"""
import threading
from mcp.mcp_config import get_enabled_servers
from mcp.mcp_client import create_mcp_client


class MCPManager:
    """单例，管理所有 MCP Server 客户端的生命周期和工具分发。"""

    def __init__(self):
        self._clients: dict = {}
        self._lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------ #
    #  初始化
    # ------------------------------------------------------------------ #

    def _ensure_initialized(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._load_servers()
            self._initialized = True

    def _load_servers(self):
        """根据 mcp.json 配置实例化客户端，type 字段决定使用哪种协议。"""
        servers = get_enabled_servers()
        for name, cfg in servers.items():
            try:
                self._clients[name] = create_mcp_client(name, cfg)
            except Exception:
                pass

    def reload(self):
        """重新加载 mcp.json 配置（热更新，无需重启服务）。"""
        with self._lock:
            self._clients.clear()
            self._initialized = False
        self._ensure_initialized()

    # ------------------------------------------------------------------ #
    #  工具定义
    # ------------------------------------------------------------------ #

    def get_tool_definitions(self) -> list[dict]:
        """返回所有已启用 MCP Server 的工具定义（OpenAI function calling 格式）。"""
        self._ensure_initialized()
        all_tools = []
        for client in self._clients.values():
            try:
                all_tools.extend(client.list_tools())
            except Exception:
                continue
        return all_tools

    def is_mcp_tool(self, tool_name: str) -> bool:
        return tool_name.startswith('mcp__')

    # ------------------------------------------------------------------ #
    #  工具调用
    # ------------------------------------------------------------------ #

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """
        调用 MCP 工具，自动路由到对应 Server。
        tool_name 格式：mcp__<server_name>__<original_tool_name>
        """
        self._ensure_initialized()

        parts = tool_name.split('__', 2)
        if len(parts) != 3 or parts[0] != 'mcp':
            return f'[MCP] 无效工具名格式: {tool_name}（应为 mcp__server__tool）'

        server_name = parts[1]
        original_name = parts[2]

        client = self._clients.get(server_name)
        if not client:
            return f'[MCP] 未找到 Server "{server_name}"，请检查 mcp.json 配置'

        return client.call_tool(original_name, arguments)

    # ------------------------------------------------------------------ #
    #  状态查询（供前端 /api/ai/mcp/servers 接口使用）
    # ------------------------------------------------------------------ #

    def get_servers_status(self) -> list[dict]:
        """返回所有 Server 的状态信息和工具列表。"""
        self._ensure_initialized()
        result = []
        for name, client in self._clients.items():
            tools = []
            error = None
            try:
                tools = client.list_tools()
            except Exception as e:
                error = str(e)

            result.append({
                'name': name,
                'type': getattr(client, 'server_type', type(client).__name__),
                'url': getattr(client, 'url', getattr(client, 'command', '')),
                'enabled': True,
                'tool_count': len(tools),
                'error': error,
                'tools': [
                    {
                        'name': t['function']['name'],
                        'description': t['function']['description']
                    }
                    for t in tools
                ]
            })
        return result


# 全局单例
mcp_manager = MCPManager()
