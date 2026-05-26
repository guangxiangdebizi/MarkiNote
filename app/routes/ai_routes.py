"""AI 助手路由：对话、备份、回滚"""
import os
import json
import uuid
from datetime import datetime
from flask import Blueprint, Response, request, jsonify, current_app
from app.auth import active_library_dir, backups_dir, conversations_dir, current_user, login_required_response, require_login, user_root, ensure_user_workspace
from app.utils.ai_provider import stream_chat_completion, get_providers_info, validate_api_key, resolve_api_key, PROVIDERS
from app.utils.ai_tools import TOOL_DEFINITIONS, SYSTEM_PROMPT, execute_tool, get_system_prompt, get_all_tool_definitions, execute_mcp_tool
from app.utils.ai_backup import BackupManager
import requests as http_requests

ai_bp = Blueprint('ai', __name__)

CONVERSATIONS_DIR = '.ai_conversations'
BACKUPS_DIR = '.ai_backups'
MAX_TOOL_ITERATIONS = 15


def _safe_library_path(base_path, rel_path):
    base_abs = os.path.abspath(base_path)
    full_path = os.path.abspath(os.path.join(base_path, rel_path or ''))
    if full_path != base_abs and not full_path.startswith(base_abs + os.sep):
        return None
    return full_path


def _get_backup_manager(lib_dir=None, backup_dir=None):
    if lib_dir is None:
        lib_dir = active_library_dir(require_login=True)
    if backup_dir is None:
        backup_dir = backups_dir()
    if not lib_dir or not backup_dir:
        return None
    return BackupManager(backup_dir, lib_dir)


def _conversations_dir(explicit_dir=None):
    if explicit_dir:
        os.makedirs(explicit_dir, exist_ok=True)
        return explicit_dir
    cdir = conversations_dir()
    if not cdir:
        return None
    os.makedirs(cdir, exist_ok=True)
    return cdir


def _load_conversation(conv_id, conv_dir=None):
    conv_dir = _conversations_dir(conv_dir)
    if not conv_dir:
        return None
    path = os.path.join(conv_dir, f'{conv_id}.json')
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def _save_conversation(conv, conv_dir=None):
    conv_dir = _conversations_dir(conv_dir)
    if not conv_dir:
        return
    path = os.path.join(conv_dir, f'{conv["id"]}.json')
    conv['updated_at'] = datetime.now().isoformat()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(conv, f, ensure_ascii=False, indent=2)


def _resolve_user_paths():
    """在请求上下文中解析用户路径，供 SSE 生成器使用（生成器内不能访问 session）。"""
    user = current_user()
    if not user:
        return None, None, None, None
    ensure_user_workspace(user['id'])
    root = user_root(user['id'])
    return (
        os.path.join(root, 'conversations'),
        os.path.join(root, 'library'),
        os.path.join(root, 'backups'),
        user['id'],
    )


def _sse_event(event_type, data):
    return f'event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


_TITLE_PROMPTS = {
    'zh-CN': '根据以下对话内容，生成一个简短的中文标题（不超过15个字）。只返回标题文本，不要加引号、标点或其他格式。',
    'en': 'Generate a short English title (max 8 words) based on the following conversation. Return only the title text, no quotes or formatting.',
    'fr': "Générez un titre court en français (max 8 mots). Retournez uniquement le texte du titre, sans guillemets.",
    'ja': '以下の会話内容に基づいて、簡潔な日本語タイトル（15文字以内）を生成してください。タイトルのみ返してください。',
}


def _generate_title(user_msg, assistant_msg, provider_id, model_id, api_key, language='zh-CN'):
    """利用 AI 根据对话内容生成简短标题"""
    try:
        from app.utils.ai_provider import PROVIDERS
        provider = PROVIDERS.get(provider_id)
        if not provider:
            return None
        url = f"{provider['base_url']}/chat/completions"
        title_model = model_id
        if provider_id == 'kimi':
            title_model = 'moonshot-v1-8k'
        title_prompt = _TITLE_PROMPTS.get(language, _TITLE_PROMPTS['zh-CN'])
        resp = http_requests.post(url, headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }, json={
            'model': title_model,
            'messages': [
                {'role': 'system', 'content': title_prompt},
                {'role': 'user', 'content': f'用户: {user_msg[:200]}\nAI: {(assistant_msg or "")[:300]}'}
            ],
            'max_tokens': 30,
            'temperature': 0.3
        }, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            title = data['choices'][0]['message']['content'].strip().strip('"\'""''')
            return title[:30]
    except Exception:
        pass
    return None


def _strip_messages_for_api(messages):
    """清理消息以符合 API 要求，移除内部字段"""
    cleaned = []
    for msg in messages:
        m = {k: v for k, v in msg.items() if k in ('role', 'content', 'tool_calls', 'tool_call_id', 'name')}
        reasoning = msg.get('_reasoning') or msg.get('reasoning_content')
        if reasoning and m.get('role') == 'assistant':
            m['reasoning_content'] = reasoning
        if m.get('role') == 'assistant' and m.get('tool_calls'):
            if not m.get('content'):
                m['content'] = None
            m['tool_calls'] = [
                {
                    'id': tc['id'],
                    'type': 'function',
                    'function': {'name': tc['function']['name'], 'arguments': tc['function']['arguments']}
                }
                for tc in m['tool_calls']
            ]
        cleaned.append(m)
    return cleaned


@ai_bp.route('/api/ai/providers', methods=['GET'])
@require_login
def get_providers():
    return jsonify({'success': True, 'providers': get_providers_info()})


@ai_bp.route('/api/ai/validate-key', methods=['POST'])
@require_login
def validate_key():
    data = request.get_json()
    provider = data.get('provider', '')
    api_key = resolve_api_key(provider, data.get('api_key', ''))
    if not provider:
        return jsonify({'success': False, 'message': '缺少参数'}), 400
    if not api_key:
        return jsonify({'success': False, 'message': '请先设置 API Key'}), 400
    ok, msg = validate_api_key(provider, api_key)
    return jsonify({'success': ok, 'message': msg})


@ai_bp.route('/api/ai/conversations', methods=['GET'])
@require_login
def list_conversations():
    conv_dir = _conversations_dir()
    convs = []
    for fname in os.listdir(conv_dir):
        if not fname.endswith('.json'):
            continue
        try:
            with open(os.path.join(conv_dir, fname), 'r', encoding='utf-8') as f:
                c = json.load(f)
            convs.append({
                'id': c['id'],
                'title': c.get('title', '新对话'),
                'created_at': c.get('created_at', ''),
                'updated_at': c.get('updated_at', ''),
                'message_count': len([m for m in c.get('messages', []) if m['role'] in ('user', 'assistant')])
            })
        except Exception:
            continue
    convs.sort(key=lambda x: x.get('updated_at', ''), reverse=True)
    return jsonify({'success': True, 'conversations': convs})


@ai_bp.route('/api/ai/conversations/<conv_id>', methods=['GET', 'DELETE', 'PATCH'])
@require_login
def manage_conversation(conv_id):
    if request.method == 'DELETE':
        path = os.path.join(_conversations_dir(), f'{conv_id}.json')
        if os.path.exists(path):
            bm = _get_backup_manager()
            removed_count = bm.delete_conversation_backups(conv_id) if bm else 0
            os.remove(path)
            return jsonify({'success': True, 'backups_removed': removed_count})
        return jsonify({'error': '对话不存在'}), 404

    if request.method == 'PATCH':
        data = request.get_json()
        conv = _load_conversation(conv_id)
        if not conv:
            return jsonify({'error': '对话不存在'}), 404
        new_title = data.get('title', '').strip()
        if new_title:
            conv['title'] = new_title[:50]
            _save_conversation(conv)
        return jsonify({'success': True, 'title': conv['title']})

    conv = _load_conversation(conv_id)
    if not conv:
        return jsonify({'error': '对话不存在'}), 404

    display_messages = []
    for m in conv.get('messages', []):
        if m['role'] == 'system':
            continue
        dm = {'role': m['role'], 'content': m.get('content', '')}
        if m.get('tool_calls'):
            dm['tool_calls'] = m['tool_calls']
        if m.get('_tool_meta'):
            dm['tool_meta'] = m['_tool_meta']
        if m.get('_reasoning'):
            dm['reasoning'] = m['_reasoning']
        display_messages.append(dm)

    return jsonify({'success': True, 'conversation': {
        'id': conv['id'],
        'title': conv.get('title', '新对话'),
        'messages': display_messages
    }})


@ai_bp.route('/api/ai/chat', methods=['POST'])
@require_login
def chat():
    data = request.get_json()
    user_message = data.get('message', '').strip()
    conv_id = data.get('conversation_id', '')
    provider_id = data.get('provider', 'deepseek')
    provider_cfg = PROVIDERS.get(provider_id, {})
    model_id = data.get('model') or provider_cfg.get('default_model', 'deepseek-v4-pro')
    api_key = resolve_api_key(provider_id, data.get('api_key', ''))
    context_file = data.get('context_file', '')
    language = data.get('language', 'zh-CN')

    if not user_message:
        return jsonify({'error': '消息不能为空'}), 400
    if not api_key:
        return jsonify({'error': '请先设置 API Key'}), 400

    conv = None
    if conv_id:
        conv = _load_conversation(conv_id)
    if not conv:
        conv_id = uuid.uuid4().hex[:12]
        conv = {
            'id': conv_id,
            'title': user_message[:30],
            'created_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat(),
            'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}]
        }

    actual_user_content = user_message
    if context_file:
        actual_user_content += f'\n\n[当前打开的文件: {context_file}]'

    attached_files = data.get('attached_files', [])
    if attached_files:
        _lib = active_library_dir(require_login=True)
        for fpath in attached_files[:5]:
            _fp = _safe_library_path(_lib, fpath.replace('\\', '/').strip('/'))
            if _fp and os.path.isfile(_fp):
                try:
                    with open(_fp, 'r', encoding='utf-8') as f:
                        _fc = f.read()
                    actual_user_content += f'\n\n[附加文件: {fpath}]\n```\n{_fc}\n```'
                except Exception:
                    pass

    conv['messages'].append({'role': 'user', 'content': actual_user_content})

    user_conv_dir, lib_dir, backup_dir, _user_id = _resolve_user_paths()
    if not user_conv_dir or not lib_dir or not backup_dir:
        return login_required_response()

    _save_conversation(conv, user_conv_dir)

    bm = _get_backup_manager(lib_dir, backup_dir)
    if not bm:
        return login_required_response()

    def generate():
        nonlocal conv
        yield _sse_event('conversation_id', {'id': conv_id})

        backup_group_id = None
        messages_for_api = _strip_messages_for_api(conv['messages'])
        if messages_for_api and messages_for_api[0]['role'] == 'system':
            messages_for_api[0]['content'] = get_system_prompt(language)

        all_tool_defs = get_all_tool_definitions()

        for iteration in range(MAX_TOOL_ITERATIONS):
            assistant_content = ''
            assistant_reasoning = ''
            tool_calls_map = {}

            for event in stream_chat_completion(
                messages_for_api, all_tool_defs, api_key, provider_id, model_id
            ):
                etype = event['type']
                if etype == 'content':
                    assistant_content += event['content']
                    yield _sse_event('token', {'content': event['content']})
                elif etype == 'reasoning':
                    assistant_reasoning += event['content']
                    yield _sse_event('reasoning', {'content': event['content']})
                elif etype == 'tool_call_start':
                    idx = event['index']
                    tool_calls_map[idx] = {
                        'id': event['id'],
                        'type': 'function',
                        'function': {'name': event['name'], 'arguments': ''}
                    }
                    yield _sse_event('tool_call', {
                        'call_id': event['id'],
                        'name': event['name'],
                        'args': {},
                        'backup_group_id': None
                    })
                elif etype == 'tool_call_args':
                    idx = event['index']
                    if idx in tool_calls_map:
                        tool_calls_map[idx]['function']['arguments'] += event['arguments']
                elif etype == 'error':
                    yield _sse_event('error', {'message': event['message']})
                    _save_conversation(conv, user_conv_dir)
                    return
                elif etype in ('done', 'tool_calls_complete'):
                    break

            tool_calls = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]

            if tool_calls:
                asst_msg = {'role': 'assistant', 'content': assistant_content or '', 'tool_calls': tool_calls}
                if assistant_reasoning:
                    asst_msg['_reasoning'] = assistant_reasoning
                conv['messages'].append(asst_msg)
                messages_for_api.append(_strip_messages_for_api([asst_msg])[0])
            else:
                asst_msg = {'role': 'assistant', 'content': assistant_content or ''}
                if assistant_reasoning:
                    asst_msg['_reasoning'] = assistant_reasoning
                conv['messages'].append(asst_msg)
                break

            if not backup_group_id:
                backup_group_id = bm.create_operation_group(conv_id)

            for tc in tool_calls:
                fn_name = tc['function']['name']
                fn_args_str = tc['function']['arguments']

                try:
                    fn_args = json.loads(fn_args_str)
                except json.JSONDecodeError:
                    fn_args = {}

                if fn_name.startswith('mcp__'):
                    result, backup_info = execute_mcp_tool(fn_name, fn_args)
                    # MCP 工具不截断，保留完整数据
                    display_result = result
                    context_result = result
                else:
                    result, backup_info = execute_tool(
                        fn_name, fn_args, lib_dir, bm, backup_group_id,
                        api_key=api_key, provider_id=provider_id, model_id=model_id
                    )
                    display_result = result[:3000]
                    context_result = result[:5000]

                yield _sse_event('tool_result', {
                    'call_id': tc['id'],
                    'name': fn_name,
                    'args': fn_args,
                    'result': display_result,
                    'backup_info': backup_info,
                    'backup_group_id': backup_group_id
                })

                tool_msg = {
                    'role': 'tool',
                    'tool_call_id': tc['id'],
                    'content': context_result,
                    '_tool_meta': {
                        'name': fn_name,
                        'args': fn_args,
                        'backup_info': backup_info,
                        'backup_group_id': backup_group_id
                    }
                }
                conv['messages'].append(tool_msg)
                messages_for_api.append({
                    'role': 'tool',
                    'tool_call_id': tc['id'],
                    'content': context_result
                })

            _save_conversation(conv, user_conv_dir)

        if backup_group_id:
            bm.cleanup()

        user_msgs = [m for m in conv['messages'] if m['role'] == 'user']
        if len(user_msgs) == 1:
            title = _generate_title(user_message, assistant_content, provider_id, model_id, api_key, language)
            if title:
                conv['title'] = title
                yield _sse_event('title_generated', {'title': title})

        _save_conversation(conv, user_conv_dir)
        yield _sse_event('done', {'conversation_id': conv_id})

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


@ai_bp.route('/api/ai/conversations/<conv_id>/save-partial', methods=['POST'])
@require_login
def save_partial(conv_id):
    """当用户中断流式输出时，保存已接收的部分内容"""
    data = request.get_json()
    partial_content = data.get('content', '')
    partial_reasoning = data.get('reasoning', '')

    conv = _load_conversation(conv_id)
    if not conv:
        return jsonify({'error': '对话不存在'}), 404

    if not partial_content and not partial_reasoning:
        return jsonify({'success': True, 'message': '无内容可保存'})

    msgs = conv.get('messages', [])
    if msgs and msgs[-1]['role'] == 'assistant' and not msgs[-1].get('tool_calls'):
        msgs[-1]['content'] = partial_content
        if partial_reasoning:
            msgs[-1]['_reasoning'] = partial_reasoning
    else:
        asst_msg = {'role': 'assistant', 'content': partial_content or '（已中断）'}
        if partial_reasoning:
            asst_msg['_reasoning'] = partial_reasoning
        conv['messages'].append(asst_msg)

    _save_conversation(conv)
    return jsonify({'success': True})


@ai_bp.route('/api/ai/conversations/<conv_id>/truncate', methods=['POST'])
@require_login
def truncate_conversation(conv_id):
    """截断对话到指定用户消息位置，并回滚之后的所有文件操作"""
    data = request.get_json()
    user_msg_number = data.get('user_msg_number')
    include_user_msg = data.get('include_user_msg', True)

    if user_msg_number is None:
        return jsonify({'error': '缺少 user_msg_number 参数'}), 400

    conv = _load_conversation(conv_id)
    if not conv:
        return jsonify({'error': '对话不存在'}), 404

    messages = conv.get('messages', [])

    count = -1
    truncate_at = len(messages)
    for i, msg in enumerate(messages):
        if msg['role'] == 'user':
            count += 1
            if count == user_msg_number:
                truncate_at = (i + 1) if include_user_msg else i
                break

    if truncate_at >= len(messages):
        return jsonify({'success': True, 'message': '无需截断', 'rollback_results': []})

    removed = messages[truncate_at:]

    backup_group_ids = []
    seen = set()
    for msg in removed:
        meta = msg.get('_tool_meta', {})
        gid = meta.get('backup_group_id')
        if gid and gid not in seen:
            seen.add(gid)
            backup_group_ids.append(gid)

    bm = _get_backup_manager()
    rollback_results = []
    for gid in reversed(backup_group_ids):
        ok, msg_text = bm.rollback_operation(gid)
        rollback_results.append({'group_id': gid, 'success': ok, 'message': msg_text})

    conv['messages'] = messages[:truncate_at]
    _save_conversation(conv)

    return jsonify({
        'success': True,
        'message': f'已截断对话，回滚了 {len(rollback_results)} 组操作',
        'rollback_results': rollback_results,
        'remaining_messages': len(conv['messages'])
    })


@ai_bp.route('/api/ai/rollback', methods=['POST'])
@require_login
def rollback():
    data = request.get_json()
    group_id = data.get('backup_group_id', '')
    op_index = data.get('operation_index')

    if not group_id:
        return jsonify({'error': '缺少备份组 ID'}), 400

    bm = _get_backup_manager()
    if not bm:
        return login_required_response()
    ok, msg = bm.rollback_operation(group_id, op_index)
    return jsonify({'success': ok, 'message': msg})


@ai_bp.route('/api/ai/backups', methods=['GET'])
@require_login
def list_backups():
    bm = _get_backup_manager()
    if not bm:
        return login_required_response()
    backups = bm.list_backups()
    return jsonify({'success': True, 'backups': backups})


@ai_bp.route('/api/ai/mcp/servers', methods=['GET'])
@require_login
def get_mcp_servers():
    """返回所有 MCP Server 的状态和工具列表。"""
    try:
        from mcp.mcp_manager import mcp_manager
        servers = mcp_manager.get_servers_status()
        return jsonify({'success': True, 'servers': servers})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'servers': []})


@ai_bp.route('/api/ai/mcp/reload', methods=['POST'])
@require_login
def reload_mcp():
    """重新加载 mcp.json 配置（热更新）。"""
    try:
        from mcp.mcp_manager import mcp_manager
        mcp_manager.reload()
        servers = mcp_manager.get_servers_status()
        return jsonify({'success': True, 'servers': servers})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})
