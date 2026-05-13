import os
from datetime import timedelta
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix

db           = SQLAlchemy()
login_manager = LoginManager()
oauth        = OAuth()


def create_app():
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

    db_url = os.environ.get('DATABASE_URL', 'sqlite:///hallpass.db')
    if db_url.startswith('postgres://'):           # Railway gives postgres://, SQLAlchemy needs postgresql://
        db_url = db_url.replace('postgres://', 'postgresql://', 1)
    app.config['SQLALCHEMY_DATABASE_URI']        = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    app.config['REMEMBER_COOKIE_DURATION']  = timedelta(days=30)
    app.config['REMEMBER_COOKIE_HTTPONLY']  = True
    app.config['REMEMBER_COOKIE_SECURE']    = not app.debug
    app.config['SESSION_COOKIE_SECURE']     = not app.debug

    db.init_app(app)
    login_manager.init_app(app)
    oauth.init_app(app)

    login_manager.login_view    = 'auth.login'
    login_manager.login_message = 'Please sign in with your Google account.'

    oauth.register(
        name='google',
        client_id=os.environ.get('GOOGLE_CLIENT_ID'),
        client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )

    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        return User.query.get(int(user_id))

    from auth   import auth_bp
    from routes import main_bp, is_nurse_viewer
    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)

    @app.context_processor
    def inject_globals():
        from flask_login import current_user
        return {'is_nurse_viewer': is_nurse_viewer(current_user)}

    with app.app_context():
        db.create_all()
        _apply_lightweight_migrations()

    return app


def _apply_lightweight_migrations():
    """Idempotent column-adds that db.create_all() won't apply to existing tables."""
    from sqlalchemy import inspect, text
    inspector = inspect(db.engine)
    cols = {c['name'] for c in inspector.get_columns('teacher_students')}
    if 'period' not in cols:
        with db.engine.begin() as conn:
            conn.execute(text('ALTER TABLE teacher_students ADD COLUMN period VARCHAR(50)'))
            # Existing rows pre-date period tracking — backfill to "1st Period"
            # since that was the only class on the roster during initial testing.
            conn.execute(text("UPDATE teacher_students SET period = '1st Period' WHERE period IS NULL"))


app = create_app()

if __name__ == '__main__':
    app.run(debug=True)
