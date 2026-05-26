"""AI 备份与回滚管理（元数据存 SQLite，文件快照存磁盘）。"""
import os
import shutil
import uuid
from datetime import datetime

from app.utils import ai_db


class BackupManager:
    def __init__(self, backup_dir, library_dir, user_id):
        self.backup_dir = backup_dir
        self.library_dir = library_dir
        self.user_id = user_id
        os.makedirs(backup_dir, exist_ok=True)

    def _validate_path(self, rel_path):
        full = os.path.abspath(os.path.join(self.library_dir, rel_path))
        if not full.startswith(os.path.abspath(self.library_dir)):
            raise ValueError(f'路径越界: {rel_path}')
        return full

    def create_operation_group(self, conversation_id=None):
        group_id = datetime.now().strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:8]
        group_dir = os.path.join(self.backup_dir, group_id)
        os.makedirs(os.path.join(group_dir, 'before'), exist_ok=True)
        os.makedirs(os.path.join(group_dir, 'after'), exist_ok=True)
        ai_db.create_backup_group(self.user_id, group_id, conversation_id)
        return group_id

    def backup_before_modify(self, group_id, operation_type, rel_path, description=''):
        group_dir = os.path.join(self.backup_dir, group_id)
        manifest = ai_db.load_backup_manifest(group_id)
        if not manifest:
            ai_db.create_backup_group(self.user_id, group_id)
            manifest = ai_db.load_backup_manifest(group_id) or {'operations': []}

        full_path = os.path.join(self.library_dir, rel_path)
        has_backup = False

        if os.path.exists(full_path):
            backup_path = os.path.join(group_dir, 'before', rel_path.replace('/', os.sep))
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            if os.path.isfile(full_path):
                shutil.copy2(full_path, backup_path)
                has_backup = True
            elif os.path.isdir(full_path):
                if os.path.exists(backup_path):
                    shutil.rmtree(backup_path)
                shutil.copytree(full_path, backup_path)
                has_backup = True

        op = {
            'index': len(manifest['operations']),
            'type': operation_type,
            'path': rel_path,
            'description': description,
            'has_backup': has_backup,
            'timestamp': datetime.now().isoformat()
        }
        ai_db.append_backup_operation(group_id, op)
        return op['index']

    def backup_after_modify(self, group_id, rel_path):
        group_dir = os.path.join(self.backup_dir, group_id)
        full_path = os.path.join(self.library_dir, rel_path)
        if os.path.exists(full_path):
            after_path = os.path.join(group_dir, 'after', rel_path.replace('/', os.sep))
            os.makedirs(os.path.dirname(after_path), exist_ok=True)
            if os.path.isfile(full_path):
                shutil.copy2(full_path, after_path)
            elif os.path.isdir(full_path):
                if os.path.exists(after_path):
                    shutil.rmtree(after_path)
                shutil.copytree(full_path, after_path)

    def rollback_operation(self, group_id, operation_index=None):
        group_dir = os.path.join(self.backup_dir, group_id)
        if not os.path.exists(group_dir):
            return False, '备份不存在'

        manifest = ai_db.load_backup_manifest(group_id)
        if not manifest:
            return False, '备份不存在'

        ops = manifest['operations']
        if operation_index is not None:
            ops = [op for op in ops if op['index'] == operation_index]
            if not ops:
                return False, '操作不存在'

        for op in reversed(ops):
            if not op.get('has_backup'):
                target = os.path.join(self.library_dir, op['path'])
                if op['type'] == 'create_file' and os.path.isfile(target):
                    os.remove(target)
                elif op['type'] == 'create_folder' and os.path.isdir(target):
                    shutil.rmtree(target)
                continue

            backup_src = os.path.join(group_dir, 'before', op['path'].replace('/', os.sep))
            target = os.path.join(self.library_dir, op['path'])

            if not os.path.exists(backup_src):
                continue

            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.isfile(backup_src):
                shutil.copy2(backup_src, target)
            elif os.path.isdir(backup_src):
                if os.path.exists(target):
                    shutil.rmtree(target)
                shutil.copytree(backup_src, target)

        return True, '回滚成功'

    def list_backups(self, limit=50):
        return ai_db.list_backup_groups(self.user_id, limit)

    def delete_conversation_backups(self, conversation_id):
        if not conversation_id:
            return 0
        group_ids = ai_db.delete_backup_groups_for_conversation(self.user_id, conversation_id)
        removed = 0
        for group_id in group_ids:
            group_dir = os.path.join(self.backup_dir, group_id)
            if os.path.isdir(group_dir):
                shutil.rmtree(group_dir, ignore_errors=True)
                removed += 1
        return removed

    def cleanup(self, max_count=100):
        remove_ids = ai_db.cleanup_old_backup_groups(self.user_id, max_count)
        for group_id in remove_ids:
            group_dir = os.path.join(self.backup_dir, group_id)
            if os.path.isdir(group_dir):
                shutil.rmtree(group_dir, ignore_errors=True)
