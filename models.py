import uuid
from datetime import datetime
from flask_login import UserMixin
from app import db


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id         = db.Column(db.Integer, primary_key=True)
    google_id  = db.Column(db.String(100), unique=True, nullable=False)
    email      = db.Column(db.String(200), unique=True, nullable=False)
    name       = db.Column(db.String(200), nullable=False)
    role       = db.Column(db.String(20), default='teacher')  # 'teacher' | 'admin'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Student(db.Model):
    __tablename__ = 'students'
    id         = db.Column(db.Integer, primary_key=True)
    token      = db.Column(db.String(36), unique=True, nullable=False,
                           default=lambda: str(uuid.uuid4()))
    first_name = db.Column(db.String(100), nullable=False)
    last_name  = db.Column(db.String(100), nullable=False)
    grade      = db.Column(db.Integer, nullable=False)
    is_blocked = db.Column(db.Boolean, nullable=False, default=False)
    block_note = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def full_name(self):
        return f'{self.first_name} {self.last_name}'


class TeacherStudent(db.Model):
    """Roster — which students belong to which teacher's class."""
    __tablename__ = 'teacher_students'
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.id'),    primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), primary_key=True)
    period     = db.Column(db.String(50))  # class period for this teacher–student pairing; NULL = unassigned


class Pass(db.Model):
    __tablename__ = 'passes'
    id               = db.Column(db.Integer, primary_key=True)
    student_id       = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    teacher_id       = db.Column(db.Integer, db.ForeignKey('users.id'),    nullable=False)
    pass_type        = db.Column(db.String(50), nullable=False)
    time_out         = db.Column(db.DateTime)           # UTC; null until student leaves
    time_in          = db.Column(db.DateTime)           # UTC; null while student is out
    duration_minutes = db.Column(db.Integer)
    period           = db.Column(db.String(50))
    extra_data       = db.Column(db.JSON)               # nurse: symptoms/interventions; visit: teacher name
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', backref='passes')
    teacher = db.relationship('User',    backref='passes_given')


class Config(db.Model):
    __tablename__ = 'config'
    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.String(500), nullable=False)


class EmergencyCheckin(db.Model):
    __tablename__ = 'emergency_checkins'
    id         = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.id'),    nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    student = db.relationship('Student', backref='emergency_checkins')
    teacher = db.relationship('User',    backref='emergency_checkins_made')
