"""读取并管理 mcp.json 配置"""
import os
import json

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'mcp.json')


def load_mcp_config() -> dict:
    """加载 mcp.json，返回配置字典。文件不存在时返回空配置。"""
    if not os.path.isfile(_CONFIG_PATH):
        return {'mcpServers': {}}
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = json.load(f)
        return config if isinstance(config, dict) else {'mcpServers': {}}
    except (json.JSONDecodeError, OSError):
        return {'mcpServers': {}}


def get_enabled_servers() -> dict:
    """返回所有 enabled=true 的 server 配置，key 为 server 名称。"""
    config = load_mcp_config()
    servers = config.get('mcpServers', {})
    return {
        name: cfg
        for name, cfg in servers.items()
        if cfg.get('enabled', True)
    }


def get_server_config(server_name: str) -> dict | None:
    """获取指定 server 的配置，不存在返回 None。"""
    servers = get_enabled_servers()
    return servers.get(server_name)
