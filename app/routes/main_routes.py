"""主要路由：首页和文件上传"""
import os
from flask import Blueprint, render_template, request, jsonify
from app.auth import active_library_dir
from app.utils import process_markdown

main_bp = Blueprint('main', __name__)


def safe_library_path(base_path, rel_path):
    base_abs = os.path.abspath(base_path)
    full_path = os.path.abspath(os.path.join(base_path, rel_path or ''))
    if full_path != base_abs and not full_path.startswith(base_abs + os.sep):
        return None
    return full_path


@main_bp.route('/')
def index():
    """主页"""
    return render_template('index.html')

@main_bp.route('/api/preview', methods=['POST'])
def preview_file():
    """预览Markdown文件"""
    data = request.get_json()
    file_path = data.get('path', '')
    
    if not file_path:
        return jsonify({'error': '文件路径不能为空'}), 400
    
    base_path = active_library_dir(require_login=False)
    full_path = safe_library_path(base_path, file_path)

    # 安全检查
    if not full_path:
        return jsonify({'error': '非法路径'}), 403
    
    try:
        if not os.path.exists(full_path):
            return jsonify({'error': '文件不存在'}), 404
        
        # 读取并渲染Markdown
        with open(full_path, 'r', encoding='utf-8') as f:
            md_content = f.read()
        
        # 处理Markdown内容
        html_content = process_markdown(md_content)
        
        return jsonify({
            'success': True,
            'html': html_content,
            'raw_markdown': md_content,
            'filename': os.path.basename(file_path)
        })
    except Exception as e:
        return jsonify({'error': f'预览失败: {str(e)}'}), 500

