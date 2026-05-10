import os
from datetime import timedelta
from flask import Blueprint, redirect, url_for, flash, render_template
from flask_login import login_user, logout_user, login_required
from app import db, oauth
from models import User

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login')
def login():
    redirect_uri = url_for('auth.callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route('/auth/callback')
def callback():
    token    = oauth.google.authorize_access_token()
    userinfo = token.get('userinfo')

    if not userinfo:
        flash('Login failed — please try again.')
        return redirect(url_for('auth.login'))

    user = User.query.filter_by(google_id=userinfo['sub']).first()
    if not user:
        admin_email = os.environ.get('ADMIN_EMAIL', '').lower()
        role = 'admin' if userinfo['email'].lower() == admin_email else 'teacher'
        user = User(
            google_id=userinfo['sub'],
            email=userinfo['email'],
            name=userinfo['name'],
            role=role,
        )
        db.session.add(user)
        db.session.commit()

    login_user(user, remember=True, duration=timedelta(days=30))
    return redirect(url_for('main.dashboard'))


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


@auth_bp.route('/login-page')
def login_page():
    return render_template('login.html')
