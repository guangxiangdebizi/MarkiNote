"""
通用 MCP 客户端
支持所有标准 MCP Server，兼容以下协议类型：
  - type=sse            : 旧版 SSE 协议 (GET /sse 建立连接，POST /messages 发送)
  - type=streamableHttp : 新版 Streamable HTTP (POST /mcp，JSON-RPC over HTTP)
  - type=stdio          : 本地子进程 (command + args 启动，JSON-RPC over stdin/stdout)

mcp.json 配置示例：
{
  "mcpServers": {
    "my-server": {
      "type": "sse",
      "url": "http://localhost:3000/sse",
      "headers": { "Authorization": "Bearer xxx" },
      "timeout": 60,
      "enabled": true
    },
    "remote-http": {
      "type": "streamableHttp",
      "url": "https://example.com/mcp",
      "headers": { "X-Api-Key": "xxx" },
      "timeout": 60
    },
    "local-process": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "some-mcp-package"],
      "env": { "API_KEY": "xxx" },
      "timeout": 60
    }
  }
}
"""
import json
import os
import subprocess
import threading
import time
import requests

class StdioMCPClient:
    """
    Stdio 类型：启动本地子进程，通过 stdin/stdout 进行 JSON-RPC 通信。
    适用于 npx、node、python 等本地命令启动的 MCP Server。
    """

    def __init__(self, server_name: str, config: dict):
        self.server_name = server_name
        self.command = config.get('command', '')
        self.args = config.get('args', [])
        self.env_extra = config.get('env', {})
        self.timeout = config.get('timeout', 60)
        self._proc = None
        self._lock = threading.Lock()
        self._req_id = 0
        self._tools_cache = None

    def _get_proc(self):
        """获取或启动子进程。"""
        if self._proc and self._proc.poll() is None:
            return self._proc
        env = {**os.environ, **self.env_extra}
        self._proc = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding='utf-8',
            bufsize=1
        )
        # 发送 initialize 握手
        self._send_request('initialize', {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'MarkiNote-MCP', 'version': '1.0'}
        })
        return self._proc

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def _send_request(self, method: str, params: dict) -> dict:
        """发送 JSON-RPC 请求并等待响应。"""
        proc = self._get_proc()
        req_id = self._next_id()
        payload = json.dumps({
            'jsonrpc': '2.0',
            'id': req_id,
            'method': method,
            'params': params
        }, ensure_ascii=False)
        proc.stdin.write(payload + '\n')
        proc.stdin.flush()

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                data = json.loads(line.strip())
                if data.get('id') == req_id:
                    return data
            except Exception:
                continue
        return {}

    def list_tools(self) -> list[dict]:
        if self._tools_cache is not None:
            return self._tools_cache
        with self._lock:
            if self._tools_cache is not None:
                return self._tools_cache
            try:
                resp = self._send_request('tools/list', {})
                self._tools_cache = _parse_tools(self.server_name, resp)
            except Exception:
                self._tools_cache = []
        return self._tools_cache

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        with self._lock:
            try:
                resp = self._send_request('tools/call', {
                    'name': tool_name,
                    'arguments': arguments
                })
                if 'result' in resp:
                    return _extract_text(resp['result'])
                elif 'error' in resp:
                    return f'[MCP:{self.server_name}] 错误: {resp["error"].get("message", "未知错误")}'
                return f'[MCP:{self.server_name}] 未收到响应'
            except Exception as e:
                return f'[MCP:{self.server_name}] 调用失败: {str(e)}'

    def invalidate_cache(self):
        self._tools_cache = None

class SSEMCPClient:
    """
    SSE 类型：通过 GET /sse 建立长连接获取 sessionId，
    再通过 POST /messages?sessionId=xxx 发送 JSON-RPC 请求。
    适用于旧版 MCP Server（如 FinanceMCP 公共云服务）。
    """

    def __init__(self, server_name: str, config: dict):
        self.server_name = server_name
        self.url = config.get('url', '').rstrip('/')
        self.extra_headers = config.get('headers', {})
        self.timeout = config.get('timeout', 60)
        self._tools_cache = None
        self._lock = threading.Lock()

    def _base_url(self) -> str:
        """去掉 /sse 后缀，得到根 URL。"""
        return self.url[:-4] if self.url.endswith('/sse') else self.url

    def _headers(self, extra: dict = None) -> dict:
        h = {'Content-Type': 'application/json', **self.extra_headers}
        if extra:
            h.update(extra)
        return h

    def _establish_session(self) -> str | None:
        """建立 SSE 连接，提取 sessionId。"""
        base = self._base_url()
        try:
            with requests.get(
                f'{base}/sse',
                headers=self._headers({'Accept': 'text/event-stream'}),
                stream=True,
                timeout=15
            ) as resp:
                if resp.status_code != 200:
                    return None
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    # 格式1: data: {"sessionId": "xxx"}
                    if line.startswith('data:'):
                        try:
                            d = json.loads(line[5:].strip())
                            if isinstance(d, dict) and 'sessionId' in d:
                                return d['sessionId']
                        except Exception:
                            pass
                    # 格式2: event: endpoint  data: /messages?sessionId=xxx
                    if line.startswith('event:') and 'endpoint' in line:
                        continue
                    if line.startswith('data:') and 'sessionId=' in line:
                        val = line[5:].strip()
                        if 'sessionId=' in val:
                            return val.split('sessionId=')[-1].split('&')[0].strip()
        except Exception:
            pass
        return None

    def _rpc(self, session_id: str, method: str, params: dict, req_id: int = 1) -> dict:
        """发送 JSON-RPC 请求到 /messages，收集 SSE 响应。"""
        base = self._base_url()
        result_box = []
        error_box = []
        done_event = threading.Event()

        def listen():
            try:
                with requests.get(
                    f'{base}/sse?sessionId={session_id}',
                    headers=self._headers({'Accept': 'text/event-stream'}),
                    stream=True,
                    timeout=self.timeout
                ) as r:
                    ev_type = None
                    for line in r.iter_lines(decode_unicode=True):
                        if line.startswith('event:'):
                            ev_type = line[6:].strip()
                        elif line.startswith('data:'):
                            if ev_type == 'message':
                                try:
                                    d = json.loads(line[5:].strip())
                                    if d.get('id') == req_id:
                                        if 'result' in d:
                                            result_box.append(d['result'])
                                        elif 'error' in d:
                                            error_box.append(d['error'])
                                        done_event.set()
                                        return
                                except Exception:
                                    pass
                            ev_type = None
            except Exception:
                done_event.set()

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        time.sleep(0.3)

        try:
            requests.post(
                f'{base}/messages?sessionId={session_id}',
                json={'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params},
                headers=self._headers(),
                timeout=self.timeout
            )
        except Exception as e:
            return {'error': {'message': str(e)}}

        done_event.wait(timeout=self.timeout)

        if result_box:
            return {'result': result_box[0]}
        if error_box:
            return {'error': error_box[0]}
        return {}

    def list_tools(self) -> list[dict]:
        if self._tools_cache is not None:
            return self._tools_cache
        with self._lock:
            if self._tools_cache is not None:
                return self._tools_cache
            try:
                session_id = self._establish_session()
                if not session_id:
                    self._tools_cache = []
                    return []
                resp = self._rpc(session_id, 'tools/list', {}, req_id=1)
                self._tools_cache = _parse_tools(self.server_name, resp)
            except Exception:
                self._tools_cache = []
        return self._tools_cache

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        try:
            session_id = self._establish_session()
            if not session_id:
                return f'[MCP:{self.server_name}] 无法建立 SSE 连接'
            resp = self._rpc(session_id, 'tools/call', {
                'name': tool_name,
                'arguments': arguments
            }, req_id=2)
            if 'result' in resp:
                return _extract_text(resp['result'])
            if 'error' in resp:
                return f'[MCP:{self.server_name}] 错误: {resp["error"].get("message", "未知错误")}'
            return f'[MCP:{self.server_name}] 未收到响应'
        except Exception as e:
            return f'[MCP:{self.server_name}] 调用失败: {str(e)}'

    def invalidate_cache(self):
        self._tools_cache = None

class StreamableHttpMCPClient:
    """
    Streamable HTTP 类型：直接 POST JSON-RPC 到 /mcp 端点。
    适用于新版 MCP Server（如 FinanceMCP v4.3+、Smithery 托管服务等）。
    """

    def __init__(self, server_name: str, config: dict):
        self.server_name = server_name
        self.url = config.get('url', '').rstrip('/')
        self.extra_headers = config.get('headers', {})
        self.timeout = config.get('timeout', 60)
        self._tools_cache = None
        self._lock = threading.Lock()

    def _headers(self) -> dict:
        return {'Content-Type': 'application/json', **self.extra_headers}

    def _rpc(self, method: str, params: dict, req_id: int = 1) -> dict:
        try:
            resp = requests.post(
                self.url,
                json={'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params},
                headers=self._headers(),
                timeout=self.timeout
            )
            if resp.status_code == 200:
                return resp.json()
            return {'error': {'message': f'HTTP {resp.status_code}: {resp.text[:200]}'}}
        except requests.Timeout:
            return {'error': {'message': f'请求超时（{self.timeout}s）'}}
        except Exception as e:
            return {'error': {'message': str(e)}}

    def list_tools(self) -> list[dict]:
        if self._tools_cache is not None:
            return self._tools_cache
        with self._lock:
            if self._tools_cache is not None:
                return self._tools_cache
            try:
                resp = self._rpc('tools/list', {})
                self._tools_cache = _parse_tools(self.server_name, resp)
            except Exception:
                self._tools_cache = []
        return self._tools_cache

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        try:
            resp = self._rpc('tools/call', {'name': tool_name, 'arguments': arguments})
            if 'result' in resp:
                return _extract_text(resp['result'])
            if 'error' in resp:
                return f'[MCP:{self.server_name}] 错误: {resp["error"].get("message", "未知错误")}'
            return f'[MCP:{self.server_name}] 未收到响应'
        except Exception as e:
            return f'[MCP:{self.server_name}] 调用失败: {str(e)}'

    def invalidate_cache(self):
        self._tools_cache = None

# ------------------------------------------------------------------ #
#  工厂函数：根据 type 字段创建对应客户端
# ------------------------------------------------------------------ #

def create_mcp_client(server_name: str, config: dict):
    """
    根据 mcp.json 中的 type 字段创建对应的 MCP 客户端实例。
    支持: sse | streamableHttp | stdio
    未知 type 默认尝试 streamableHttp。
    """
    server_type = config.get('type', 'streamableHttp').lower()
    if server_type == 'sse':
        return SSEMCPClient(server_name, config)
    elif server_type == 'stdio':
        return StdioMCPClient(server_name, config)
    else:
        return StreamableHttpMCPClient(server_name, config)


# ------------------------------------------------------------------ #
#  共享工具函数（所有客户端类共用）
# ------------------------------------------------------------------ #

def _parse_tools(server_name: str, rpc_response: dict) -> list[dict]:
    """
    将 JSON-RPC tools/list 响应转换为 OpenAI function calling 格式。
    工具名格式：mcp__<server_name>__<original_tool_name>
    """
    try:
        result = rpc_response.get('result', {})
        tools = result.get('tools', [])
        converted = []
        for tool in tools:
            name = tool.get('name', '')
            if not name:
                continue
            description = tool.get('description', '')
            input_schema = tool.get('inputSchema', {})

            # 清理 inputSchema 中 LLM 不支持的字段
            parameters = _clean_schema(input_schema) if input_schema else {
                'type': 'object',
                'properties': {},
                'required': []
            }

            converted.append({
                'type': 'function',
                'function': {
                    'name': f'mcp__{server_name}__{name}',
                    'description': f'[MCP:{server_name}] {description}',
                    'parameters': parameters
                },
                '_mcp_meta': {
                    'server': server_name,
                    'original_name': name
                }
            })
        return converted
    except Exception:
        return []


def _clean_schema(schema: dict) -> dict:
    """移除 JSON Schema 中 OpenAI API 不支持的字段（如 $schema、additionalProperties 等）。"""
    ALLOWED_KEYS = {'type', 'properties', 'required', 'description',
                    'enum', 'items', 'anyOf', 'oneOf', 'allOf', 'not',
                    'minimum', 'maximum', 'minLength', 'maxLength',
                    'default', 'format', 'title'}
    if not isinstance(schema, dict):
        return schema
    cleaned = {}
    for k, v in schema.items():
        if k not in ALLOWED_KEYS:
            continue
        if k == 'properties' and isinstance(v, dict):
            cleaned[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
        elif k == 'items' and isinstance(v, dict):
            cleaned[k] = _clean_schema(v)
        else:
            cleaned[k] = v
    return cleaned


def _extract_text(result) -> str:
    """从 MCP tools/call 响应结果中提取可读文本。"""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts = []
        for item in result:
            if isinstance(item, dict):
                if item.get('type') == 'text':
                    parts.append(item.get('text', ''))
                elif item.get('type') == 'image':
                    parts.append(f'[图片: {item.get("url", "")}]')
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return '\n'.join(p for p in parts if p)
    if isinstance(result, dict):
        content = result.get('content', result)
        if isinstance(content, list):
            return _extract_text(content)
        if isinstance(content, str):
            return content
        return json.dumps(result, ensure_ascii=False, indent=2)
    return str(result)
