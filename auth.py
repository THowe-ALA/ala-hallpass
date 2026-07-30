import os
from datetime import timedelta
from flask import Blueprint, redirect, url_for, flash, render_template, current_app
from flask_login import login_user, logout_user, login_required
from app import db, oauth
from models import User

auth_bp = Blueprint('auth', __name__)


def _allowed_teacher_emails():
    raw = os.environ.get('ALLOWED_TEACHERS', '')
    return {e.strip().lower() for e in raw.split(',') if e.strip()}


def is_allowed_teacher(email):
    """Explicit allowlist gate for who may hold an account at all.

    Fails CLOSED: an unset or empty ALLOWED_TEACHERS admits nobody but
    ADMIN_EMAIL. This used to fail OPEN so that first deploy couldn't lock the
    staff out before the env var existed, but the OAuth app is now PUBLISHED —
    Google no longer gates sign-in to a test-user list, so this allowlist is the
    only thing between the open internet and an auto-created teacher account.
    Losing the variable must not mean admitting everyone.

    ADMIN_EMAIL is always allowed, so a typo in the list can't lock out the
    admin who needs to fix the list.
    """
    email       = (email or '').strip().lower()
    admin_email = os.environ.get('ADMIN_EMAIL', '').strip().lower()
    if admin_email and email == admin_email:
        return True
    allowed = _allowed_teacher_emails()
    if not allowed:
        # Misconfiguration, not a normal rejection — make it findable in the
        # Railway deploy log instead of looking like a bad email.
        current_app.logger.error(
            'ALLOWED_TEACHERS is empty or unset — rejecting %s. '
            'Every non-admin sign-in will fail until it is restored.', email)
        return False
    return email in allowed


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

    # Checked on EVERY login, not just account creation, so removing someone
    # from ALLOWED_TEACHERS revokes an existing account instead of only
    # blocking new ones. Redirect to login_page (static) rather than login
    # (which re-fires the Google redirect) or rejected users hit a loop.
    if not is_allowed_teacher(userinfo.get('email')):
        logout_user()
        flash("That Google account isn't authorized for Muster. "
              "If you should have access, ask Ms. Howe to add you.")
        return redirect(url_for('auth.login_page'))

    user = User.query.filter_by(google_id=userinfo['sub']).first()

    # A deactivated account must not be able to sign back in, and must not be
    # silently resurrected as a brand-new account either. Reactivating is an
    # explicit admin action.
    if user is not None and not user.is_active:
        logout_user()
        flash('That account has been deactivated. '
              'If this is a mistake, ask Ms. Howe to reactivate it.')
        return redirect(url_for('auth.login_page'))

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
