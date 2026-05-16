"""邮箱验证码登录路由。"""
from flask import Blueprint, jsonify, request, session

from app.auth import (
    EMAIL_RE,
    create_email_code,
    current_user,
    get_or_create_user,
    normalize_email,
    normalize_username,
    send_email_code,
    verify_email_code,
    verify_password_user,
)

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/api/auth/me', methods=['GET'])
def me():
    user = current_user()
    return jsonify({
        'authenticated': bool(user),
        'user': user,
    })


@auth_bp.route('/api/auth/send-code', methods=['POST'])
def send_code():
    data = request.get_json() or {}
    email = normalize_email(data.get('email', ''))

    if not EMAIL_RE.match(email):
        return jsonify({'success': False, 'error': '邮箱格式不正确'}), 400

    code = create_email_code(email)
    sent, message = send_email_code(email, code)

    return jsonify({
        'success': True,
        'sent': sent,
        'message': message,
    })


@auth_bp.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    email = normalize_email(data.get('email', ''))
    code = (data.get('code', '') or '').strip()

    ok, message = verify_email_code(email, code)
    if not ok:
        return jsonify({'success': False, 'error': message}), 400

    user = get_or_create_user(email)
    session['user_id'] = user['id']
    session['email'] = user['email']
    session['username'] = user.get('username') or ''
    session.permanent = True

    return jsonify({
        'success': True,
        'message': '登录成功',
        'user': current_user(),
    })


@auth_bp.route('/api/auth/password-login', methods=['POST'])
def password_login():
    data = request.get_json() or {}
    username = normalize_username(data.get('username', ''))
    password = data.get('password', '') or ''

    user, message = verify_password_user(username, password)
    if not user:
        return jsonify({'success': False, 'error': message}), 400

    session['user_id'] = user['id']
    session['email'] = user['email']
    session['username'] = user.get('username') or ''
    session.permanent = True

    return jsonify({
        'success': True,
        'message': '登录成功',
        'user': current_user(),
    })


@auth_bp.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})
