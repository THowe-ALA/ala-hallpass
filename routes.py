import csv
import io
import os
import re
import pytz
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user
from app import db
from models import User, Student, TeacherStudent, Pass

main_bp = Blueprint('main', __name__)

TZ = pytz.timezone('America/Phoenix')

SYMPTOMS     = ['Headache', 'Dizziness', 'Bleeding', 'Stomachache',
                'Shortness of breath', 'Nausea', 'Fever']
INTERVENTIONS = ['Bandaid', 'Water', 'Put head down', 'Saltines', 'Mint', 'Ice pack']
PASS_TYPES   = [
    ('restroom',      'Restroom'),
    ('nurse',         'Nurse'),
    ('office',        'Office'),
    ('late',          'Late Departure'),
    ('teacher_visit', 'Going to Another Teacher'),
]
PASS_LABELS  = dict(PASS_TYPES)
PERIODS      = ['Zero Period', 'Leadership Period', '1st Period', '2nd Period',
                '3rd Period', '4th / Lunch / 5th', '6th Period', '7th Period',
                'Outside School Hours']


# ── Helpers ──────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated


def get_period(dt_local):
    is_wed = dt_local.weekday() == 2
    t = dt_local.hour * 60 + dt_local.minute

    def m(h, mn): return h * 60 + mn

    if is_wed:
        if m(7,20)  <= t < m(8,10):  return 'Zero Period'
        if m(8,15)  <= t < m(8,35):  return 'Leadership Period'
        if m(8,35)  <= t < m(9,22):  return '1st Period'
        if m(9,26)  <= t < m(10,13): return '2nd Period'
        if m(10,17) <= t < m(11,4):  return '3rd Period'
        if m(11,4)  <= t < m(13,15): return '4th / Lunch / 5th'
        if m(13,19) <= t < m(14,5):  return '6th Period'
        if m(14,9)  <= t < m(14,55): return '7th Period'
    else:
        if m(7,20)  <= t < m(8,10):  return 'Zero Period'
        if m(8,15)  <= t < m(9,7):   return '1st Period'
        if m(9,11)  <= t < m(10,0):  return '2nd Period'
        if m(10,4)  <= t < m(10,53): return '3rd Period'
        if m(10,53) <= t < m(13,9):  return '4th / Lunch / 5th'
        if m(13,13) <= t < m(14,2):  return '6th Period'
        if m(14,6)  <= t < m(14,55): return '7th Period'
    return 'Outside School Hours'


def _period_chips_for_teacher(teacher_id):
    """Return [(value, label, count), ...] for filter chips on roster/dashboard pages.

    `value` is what goes in the ?period= query param: None for "All",
    '__none__' for the unassigned bucket, or the period name itself.
    """
    from sqlalchemy import func
    rows = (db.session.query(TeacherStudent.period, func.count(TeacherStudent.student_id))
            .filter(TeacherStudent.teacher_id == teacher_id)
            .group_by(TeacherStudent.period)
            .all())
    by_period = {p: c for p, c in rows}
    total = sum(by_period.values())
    chips = [(None, 'All', total)]
    for p in PERIODS:
        if by_period.get(p, 0) > 0:
            chips.append((p, p, by_period[p]))
    if by_period.get(None, 0) > 0:
        chips.append(('__none__', 'Unassigned', by_period[None]))
    return chips


def _apply_period_filter(query, period_arg):
    """Add a TeacherStudent.period filter to a roster query based on the ?period= param."""
    if period_arg == '__none__':
        return query.filter(TeacherStudent.period.is_(None))
    if period_arg:
        return query.filter(TeacherStudent.period == period_arg)
    return query


def get_flags(student_id, exclude_pass_id=None):
    now_local    = datetime.now(TZ)
    today_start  = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    one_hour_ago = now_local - timedelta(hours=1)

    q = Pass.query.filter(Pass.student_id == student_id, Pass.time_out.isnot(None))
    if exclude_pass_id:
        q = q.filter(Pass.id != exclude_pass_id)

    today_count  = q.filter(Pass.time_out >= today_start.replace(tzinfo=None)).count()
    recent_flag  = q.filter(Pass.time_out >= one_hour_ago.replace(tzinfo=None)).first() is not None
    return recent_flag, (today_count >= 3), today_count


# ── Routes ───────────────────────────────────────────────────────────────────

@main_bp.route('/')
@login_required
def index():
    return redirect(url_for('main.dashboard'))


@main_bp.route('/dashboard')
@login_required
def dashboard():
    period_arg = request.args.get('period') or None
    q = (db.session.query(Student)
         .join(TeacherStudent, Student.id == TeacherStudent.student_id)
         .filter(TeacherStudent.teacher_id == current_user.id))
    q = _apply_period_filter(q, period_arg)
    roster = q.order_by(Student.last_name, Student.first_name).all()
    chips = _period_chips_for_teacher(current_user.id)

    now_local   = datetime.now(TZ)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

    rows = []
    for s in roster:
        open_pass   = (Pass.query
                       .filter_by(student_id=s.id, time_in=None)
                       .filter(Pass.time_out.isnot(None))
                       .order_by(Pass.time_out.desc())
                       .first())
        today_count = (Pass.query
                       .filter(Pass.student_id == s.id,
                               Pass.time_out >= today_start.replace(tzinfo=None))
                       .count())
        duration_so_far = None
        if open_pass:
            duration_so_far = int((datetime.utcnow() - open_pass.time_out).total_seconds() // 60)

        rows.append({
            'student':         s,
            'open_pass':       open_pass,
            'today_count':     today_count,
            'frequent_flag':   today_count >= 3,
            'duration_so_far': duration_so_far,
        })

    return render_template('dashboard.html', rows=rows, now=now_local,
                           chips=chips, active_period=period_arg)


@main_bp.route('/scan/<token>')
@login_required
def scan(token):
    student   = Student.query.filter_by(token=token).first_or_404()
    open_pass = (Pass.query
                 .filter_by(student_id=student.id, time_in=None)
                 .filter(Pass.time_out.isnot(None))
                 .order_by(Pass.time_out.desc())
                 .first())

    now_local            = datetime.now(TZ)
    recent_flag, frequent_flag, today_count = get_flags(student.id)

    duration_so_far = None
    if open_pass:
        duration_so_far = int((datetime.utcnow() - open_pass.time_out).total_seconds() // 60)

    return render_template('scan.html',
        student=student, open_pass=open_pass,
        recent_flag=recent_flag, frequent_flag=frequent_flag,
        today_count=today_count, duration_so_far=duration_so_far,
        pass_types=PASS_TYPES, symptoms=SYMPTOMS,
        interventions=INTERVENTIONS, now=now_local)


@main_bp.route('/log_out/<int:student_id>', methods=['POST'])
@login_required
def log_out(student_id):
    student   = Student.query.get_or_404(student_id)
    now_utc   = datetime.utcnow()
    now_local = datetime.now(TZ)
    pass_type = request.form.get('pass_type', 'restroom')

    extra = {}
    if pass_type == 'nurse':
        extra['symptoms']      = request.form.getlist('symptoms')
        extra['interventions'] = request.form.getlist('interventions')
    elif pass_type == 'teacher_visit':
        extra['destination_teacher'] = request.form.get('destination_teacher', '').strip()

    p = Pass(
        student_id=student.id,
        teacher_id=current_user.id,
        pass_type=pass_type,
        time_out=now_utc,
        period=get_period(now_local),
        extra_data=extra,
    )
    db.session.add(p)
    db.session.commit()
    return redirect(url_for('main.confirm', pass_id=p.id))


@main_bp.route('/log_in/<int:pass_id>', methods=['POST'])
@login_required
def log_in(pass_id):
    p             = Pass.query.get_or_404(pass_id)
    now_utc       = datetime.utcnow()
    p.time_in     = now_utc
    p.duration_minutes = max(0, int((now_utc - p.time_out).total_seconds() // 60))
    db.session.commit()
    return redirect(url_for('main.confirm', pass_id=pass_id))


@main_bp.route('/confirm/<int:pass_id>')
@login_required
def confirm(pass_id):
    p         = Pass.query.get_or_404(pass_id)
    direction = 'in' if p.time_in else 'out'
    recent_flag, frequent_flag, today_count = get_flags(p.student_id, exclude_pass_id=p.id)
    now_local = datetime.now(TZ)
    return render_template('confirm.html',
        p=p, direction=direction,
        pass_label=PASS_LABELS.get(p.pass_type, p.pass_type),
        recent_flag=recent_flag, frequent_flag=frequent_flag,
        today_count=today_count, now=now_local)


@main_bp.route('/students')
@login_required
def students():
    period_arg = request.args.get('period') or None
    if current_user.role == 'admin':
        all_students = Student.query.order_by(Student.last_name, Student.first_name).all()
        return render_template('students.html', students=all_students, is_admin=True,
                               chips=None, active_period=None, rows=None)
    q = (db.session.query(Student, TeacherStudent.period)
         .join(TeacherStudent, Student.id == TeacherStudent.student_id)
         .filter(TeacherStudent.teacher_id == current_user.id))
    q = _apply_period_filter(q, period_arg)
    roster_rows = q.order_by(Student.last_name, Student.first_name).all()
    rows = [{'student': s, 'period': p} for s, p in roster_rows]
    chips = _period_chips_for_teacher(current_user.id)
    return render_template('students.html', students=None, rows=rows, is_admin=False,
                           chips=chips, active_period=period_arg)


@main_bp.route('/students/add', methods=['GET', 'POST'])
@login_required
def add_student():
    if request.method == 'POST':
        fn     = request.form['first_name'].strip()
        ln     = request.form['last_name'].strip()
        grade  = int(request.form['grade'])
        period = request.form.get('period') or None
        if period and period not in PERIODS:
            period = None

        student = Student.query.filter_by(first_name=fn, last_name=ln, grade=grade).first()
        if not student:
            student = Student(first_name=fn, last_name=ln, grade=grade)
            db.session.add(student)
            db.session.flush()

        existing = TeacherStudent.query.filter_by(
            teacher_id=current_user.id, student_id=student.id
        ).first()
        if existing:
            if period and existing.period != period:
                existing.period = period
        else:
            db.session.add(TeacherStudent(
                teacher_id=current_user.id, student_id=student.id, period=period))

        db.session.commit()
        flash(f'{student.full_name} added to your roster.')
        return redirect(url_for('main.students'))

    return render_template('add_student.html', periods=PERIODS)


_HEADER_ALIASES = {
    'first_name': {'first_name', 'firstname', 'first', 'first name', 'given', 'given name'},
    'last_name':  {'last_name', 'lastname', 'last', 'last name', 'surname', 'family name'},
    'grade':      {'grade', 'year', 'grade level', 'level'},
}

def _normalize_header(h):
    return (h or '').strip().lower().replace('-', ' ').replace('_', ' ')

def _map_columns(fieldnames):
    """Return dict of canonical_name -> actual_column_name, or None if a required col is missing."""
    mapping = {}
    norm = {_normalize_header(f).replace(' ', '_'): f for f in (fieldnames or [])}
    # also try un-underscored
    norm2 = {_normalize_header(f): f for f in (fieldnames or [])}
    for canonical, aliases in _HEADER_ALIASES.items():
        found = None
        for alias in aliases:
            key1 = alias.replace(' ', '_')
            if key1 in norm:
                found = norm[key1]
                break
            if alias in norm2:
                found = norm2[alias]
                break
        if not found:
            return None, canonical
        mapping[canonical] = found
    return mapping, None

def _parse_grade(raw):
    """Accept '7', '7th', 'seventh', etc. Return int 7-12 or None."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    words = {'seventh': 7, 'eighth': 8, 'ninth': 9,
             'tenth': 10, 'eleventh': 11, 'twelfth': 12}
    if s in words:
        return words[s]
    digits = re.findall(r'\d+', s)
    if not digits:
        return None
    try:
        n = int(digits[0])
    except ValueError:
        return None
    return n if 7 <= n <= 12 else None


@main_bp.route('/students/upload', methods=['GET', 'POST'])
@login_required
def upload_students():
    if request.method == 'GET':
        return render_template('upload_students.html', periods=PERIODS)

    file = request.files.get('csv_file')
    if not file or file.filename == '':
        flash('Please choose a CSV file to upload.')
        return redirect(url_for('main.upload_students'))

    period = request.form.get('period') or None
    if period and period not in PERIODS:
        period = None

    try:
        raw = file.read().decode('utf-8-sig')  # handle Excel BOM
    except UnicodeDecodeError:
        flash('Could not read the file. Please save it as CSV (UTF-8) and try again.')
        return redirect(url_for('main.upload_students'))

    reader = csv.DictReader(io.StringIO(raw))
    mapping, missing = _map_columns(reader.fieldnames)
    if missing:
        flash(f"CSV is missing a '{missing}' column. Expected headers: first_name, last_name, grade.")
        return redirect(url_for('main.upload_students'))

    # Pre-load existing students into a dict keyed by (first.lower, last.lower, grade).
    existing = {}
    for s in Student.query.all():
        existing[(s.first_name.strip().lower(), s.last_name.strip().lower(), s.grade)] = s

    # Pre-load this teacher's roster as a set of student_ids.
    on_roster = {
        ts.student_id for ts in
        TeacherStudent.query.filter_by(teacher_id=current_user.id).all()
    }

    created = 0
    added_to_roster = 0
    skipped_existing = 0  # student already in DB
    already_on_roster = 0
    errors = []

    for i, row in enumerate(reader, start=2):  # row 1 is the header
        fn = (row.get(mapping['first_name']) or '').strip()
        ln = (row.get(mapping['last_name'])  or '').strip()
        grade = _parse_grade(row.get(mapping['grade']))

        if not fn or not ln:
            errors.append(f'Row {i}: missing first or last name.')
            continue
        if grade is None:
            errors.append(f'Row {i}: bad grade for {fn} {ln} (must be 7-12).')
            continue

        key = (fn.lower(), ln.lower(), grade)
        student = existing.get(key)

        if student:
            skipped_existing += 1
        else:
            student = Student(first_name=fn, last_name=ln, grade=grade)
            db.session.add(student)
            db.session.flush()  # get student.id
            existing[key] = student
            created += 1

        if student.id in on_roster:
            already_on_roster += 1
        else:
            db.session.add(TeacherStudent(
                teacher_id=current_user.id, student_id=student.id, period=period
            ))
            on_roster.add(student.id)
            added_to_roster += 1

    db.session.commit()
    return render_template(
        'upload_students.html',
        periods=PERIODS,
        results={
            'created': created,
            'added_to_roster': added_to_roster,
            'skipped_existing': skipped_existing,
            'already_on_roster': already_on_roster,
            'period': period,
            'errors': errors,
        }
    )


@main_bp.route('/students/<int:student_id>/remove', methods=['POST'])
@login_required
def remove_student(student_id):
    ts = TeacherStudent.query.filter_by(
        teacher_id=current_user.id, student_id=student_id
    ).first_or_404()
    db.session.delete(ts)
    db.session.commit()
    flash('Student removed from your roster.')
    return redirect(url_for('main.students'))


@main_bp.route('/print')
@login_required
def print_cards():
    if current_user.role == 'admin':
        roster = Student.query.order_by(Student.last_name, Student.first_name).all()
    else:
        roster = (db.session.query(Student)
                  .join(TeacherStudent, Student.id == TeacherStudent.student_id)
                  .filter(TeacherStudent.teacher_id == current_user.id)
                  .order_by(Student.last_name, Student.first_name)
                  .all())
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    return render_template('print_cards.html', roster=roster, base_url=base_url)


@main_bp.route('/admin')
@login_required
@admin_required
def admin():
    date_str  = request.args.get('date', datetime.now(TZ).strftime('%Y-%m-%d'))
    student_q = request.args.get('student', '').strip()
    period_f  = request.args.get('period', '')

    try:
        day = datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        day = datetime.now(TZ).replace(tzinfo=None)

    q = Pass.query.filter(db.func.date(Pass.time_out) == day.date())
    if student_q:
        q = q.join(Student).filter(db.or_(
            Student.first_name.ilike(f'%{student_q}%'),
            Student.last_name.ilike(f'%{student_q}%'),
        ))
    if period_f:
        q = q.filter(Pass.period == period_f)

    passes = q.order_by(Pass.time_out.desc()).all()

    from sqlalchemy import func
    frequent = (db.session.query(Student, func.count(Pass.id).label('cnt'))
                .join(Pass, Student.id == Pass.student_id)
                .filter(db.func.date(Pass.time_out) == day.date())
                .group_by(Student.id)
                .having(func.count(Pass.id) >= 3)
                .order_by(func.count(Pass.id).desc())
                .all())

    all_users = User.query.order_by(User.name).all()

    return render_template('admin.html',
        passes=passes, frequent=frequent,
        date_str=date_str, student_q=student_q, period_f=period_f,
        periods=PERIODS, pass_labels=PASS_LABELS, all_users=all_users)


@main_bp.route('/admin/promote/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def promote(user_id):
    user = User.query.get_or_404(user_id)
    user.role = 'admin'
    db.session.commit()
    flash(f'{user.name} is now an admin.')
    return redirect(url_for('main.admin'))


@main_bp.app_errorhandler(403)
def forbidden(e):
    return '<h2 style="font-family:sans-serif;text-align:center;margin-top:100px">403 — Not authorized</h2>', 403
