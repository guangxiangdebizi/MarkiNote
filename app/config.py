"""应用配置文件"""
import os
import shutil

class Config:
    """Flask应用配置"""
    SECRET_KEY = os.environ.get('MARKINOTE_SECRET_KEY')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    LIBRARY_FOLDER = 'lib'  # 默认文档模板目录
    USER_DATA_DIR = 'user_data'
    ALLOWED_EXTENSIONS = {'md', 'markdown', 'txt'}
    SEND_FILE_MAX_AGE_DEFAULT = 0  # 开发模式下不缓存静态文件

    @staticmethod
    def init_app(app):
        """初始化应用配置"""
        # 确保默认模板目录和用户数据目录存在
        os.makedirs(app.config['LIBRARY_FOLDER'], exist_ok=True)
        os.makedirs(app.config['USER_DATA_DIR'], exist_ok=True)

        # 默认文档以 README 作为公共模板；匿名用户可读，登录用户首次进入时会复制一份到个人库。
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        for name in ('README.md', 'README_EN.md'):
            src = os.path.join(project_root, name)
            dst = os.path.join(app.config['LIBRARY_FOLDER'], name)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)

        # 未配置环境变量时，把 session secret 固化到本地文件，避免每次重启踢掉所有登录态。
        if not app.config.get('SECRET_KEY'):
            secret_path = os.path.join(app.config['USER_DATA_DIR'], '.secret_key')
            if os.path.exists(secret_path):
                with open(secret_path, 'r', encoding='utf-8') as f:
                    app.config['SECRET_KEY'] = f.read().strip()
            else:
                import secrets
                app.config['SECRET_KEY'] = secrets.token_urlsafe(48)
                with open(secret_path, 'w', encoding='utf-8') as f:
                    f.write(app.config['SECRET_KEY'])

