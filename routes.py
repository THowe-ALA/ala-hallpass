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


def _nurse_viewer_emails():
    raw = os.environ.get('NURSE_VIEWERS', '')
    return {e.strip().lower() for e in raw.split(',') if e.strip()}


def is_nurse_viewer(user):
    if not user or not user.is_authenticated:
        return False
    return (user.email or '').strip().lower() in _nurse_viewer_emails()


def nurse_viewer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_nurse_viewer(current_user):
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


def _previous_school_day(today_date, today_weekday):
    """Mon → previous Friday; Tue–Fri → yesterday; weekend → None (no flag)."""
    if today_weekday == 0:                 # Monday
        return today_date - timedelta(days=3)
    if today_weekday in (5, 6):            # Saturday / Sunday
        return None
    return today_date - timedelta(days=1)


def get_same_period_streak(student_id, now_local):
    """Return (flagged_bool, current_period_or_None).

    Flagged when the student had a pass in the same period on the immediately
    preceding school day (Friday counts as previous school day for Monday).
    """
    period = get_period(now_local)
    if period == 'Outside School Hours':
        return False, None
    prev_day = _previous_school_day(now_local.date(), now_local.weekday())
    if prev_day is None:
        return False, period
    prior = (Pass.query
             .filter(Pass.student_id == student_id,
                     Pass.period == period,
                     Pass.time_out.isnot(None),
                     db.func.date(Pass.time_out) == prev_day)
             .first())
    return prior is not None, period


def get_cross_teacher_buddies(now_local, threshold=3, window_seconds=300):
    """Detect cross-class buddy pairs across the whole campus this week.

    Returns (buddy_map, buddy_pairs):
      - buddy_map: {student_id: [buddy_full_name, ...]}
      - buddy_pairs: sorted list of unique (name_a, name_b) tuples

    A "buddy pair" = two students whose passes were logged within
    `window_seconds` of each other by DIFFERENT teachers, on `threshold` or
    more separate occasions during the current school week (Mon–Fri).
    """
    weekday = now_local.weekday()
    if weekday >= 5:                       # weekend → no school week to check
        return {}, []
    week_start = now_local.date() - timedelta(days=weekday)
    week_end   = week_start + timedelta(days=5)  # exclusive

    week_passes = (Pass.query
                   .filter(Pass.time_out.isnot(None),
                           db.func.date(Pass.time_out) >= week_start,
                           db.func.date(Pass.time_out) <  week_end)
                   .order_by(Pass.time_out)
                   .all())

    pair_counts = {}                       # (student_a, student_b) → count, a<b
    n = len(week_passes)
    for i in range(n):
        pa = week_passes[i]
        for j in range(i + 1, n):
            pb = week_passes[j]
            if (pb.time_out - pa.time_out).total_seconds() > window_seconds:
                break                      # sorted, no later pass will fit window
            if pa.student_id == pb.student_id:
                continue
            if pa.teacher_id == pb.teacher_id:
                continue
            key = (min(pa.student_id, pb.student_id), max(pa.student_id, pb.student_id))
            pair_counts[key] = pair_counts.get(key, 0) + 1

    flagged_pairs = [(a, b) for (a, b), c in pair_counts.items() if c >= threshold]
    if not flagged_pairs:
        return {}, []

    all_ids = {sid for pair in flagged_pairs for sid in pair}
    name_by_id = {s.id: s.full_name for s in
                  Student.query.filter(Student.id.in_(all_ids)).all()}

    buddy_map = {}
    pair_set  = set()
    for a, b in flagged_pairs:
        na, nb = name_by_id.get(a), name_by_id.get(b)
        if not na or not nb:
            continue
        buddy_map.setdefault(a, []).append(nb)
        buddy_map.setdefault(b, []).append(na)
        pair_set.add(tuple(sorted([na, nb])))

    for sid in buddy_map:
        buddy_map[sid] = sorted(buddy_map[sid])
    return buddy_map, sorted(pair_set)


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
    same_period_flag, current_period = get_same_period_streak(student.id, now_local)

    duration_so_far = None
    if open_pass:
        duration_so_far = int((datetime.utcnow() - open_pass.time_out).total_seconds() // 60)

    return render_template('scan.html',
        student=student, open_pass=open_pass,
        recent_flag=recent_flag, frequent_flag=frequent_flag,
        same_period_flag=same_period_flag, current_period=current_period,
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
    elif pass_type == 'late':
        extra['releasing_teacher'] = request.form.get('releasing_teacher', '').strip()

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
    p       = Pass.query.get_or_404(pass_id)
    now_utc = datetime.utcnow()
    action  = request.form.get('action', 'log_in')

    if p.pass_type == 'nurse' and is_nurse_viewer(current_user):
        extra = dict(p.extra_data or {})
        extra['symptoms']      = request.form.getlist('symptoms')
        extra['interventions'] = request.form.getlist('interventions')
        notes = (request.form.get('nurse_notes') or '').strip()
        if notes:
            extra['nurse_notes'] = notes
        elif 'nurse_notes' in extra:
            extra.pop('nurse_notes')
        extra['nurse_id']     = current_user.id
        extra['nurse_email']  = current_user.email
        p.extra_data = extra
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(p, 'extra_data')

        if action == 'save':
            db.session.commit()
            flash('Nurse info saved. Student still marked OUT.')
            return redirect(url_for('main.scan', token=p.student.token))

    p.time_in          = now_utc
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
    same_period_flag, _ = get_same_period_streak(p.student_id, now_local)
    return render_template('confirm.html',
        p=p, direction=direction,
        pass_label=PASS_LABELS.get(p.pass_type, p.pass_type),
        recent_flag=recent_flag, frequent_flag=frequent_flag,
        same_period_flag=same_period_flag,
        today_count=today_count, now=now_local)


@main_bp.route('/students')
@login_required
def students():
    period_arg = request.args.get('period') or None
    view_arg   = request.args.get('view', 'mine')  # 'mine' | 'all'
    is_admin   = (current_user.role == 'admin')

    if is_admin and view_arg == 'all':
        all_students = Student.query.order_by(Student.last_name, Student.first_name).all()
        buddy_map, buddy_pairs = get_cross_teacher_buddies(datetime.now(TZ))
        return render_template('students.html', students=all_students, rows=None,
                               is_admin=True, view='all',
                               chips=None, active_period=None,
                               buddy_map=buddy_map, buddy_pairs=buddy_pairs)

    q = (db.session.query(Student, TeacherStudent.period)
         .join(TeacherStudent, Student.id == TeacherStudent.student_id)
         .filter(TeacherStudent.teacher_id == current_user.id))
    q = _apply_period_filter(q, period_arg)
    roster_rows = q.order_by(Student.last_name, Student.first_name).all()

    from sqlalchemy import func
    pass_counts = dict(
        db.session.query(Pass.student_id, func.count(Pass.id))
        .filter(Pass.teacher_id == current_user.id,
                Pass.time_out.isnot(None))
        .group_by(Pass.student_id)
        .all()
    )
    rows = [{'student': s, 'period': p, 'pass_count': pass_counts.get(s.id, 0)}
            for s, p in roster_rows]
    chips = _period_chips_for_teacher(current_user.id)
    return render_template('students.html', students=None, rows=rows,
                           is_admin=is_admin, view='mine',
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
    period_arg = request.args.get('period') or None
    # Admin with no period filter keeps the full-school card sheet;
    # otherwise filter to the current teacher's roster (optionally by period).
    if current_user.role == 'admin' and not period_arg:
        roster = Student.query.order_by(Student.last_name, Student.first_name).all()
    else:
        q = (db.session.query(Student)
             .join(TeacherStudent, Student.id == TeacherStudent.student_id)
             .filter(TeacherStudent.teacher_id == current_user.id))
        q = _apply_period_filter(q, period_arg)
        roster = q.order_by(Student.last_name, Student.first_name).all()
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    return render_template('print_cards.html', roster=roster, base_url=base_url,
                           filter_period=period_arg)


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


def _nurse_passes_query():
    return (Pass.query
            .filter(Pass.pass_type == 'nurse')
            .filter(Pass.time_out.isnot(None)))


@main_bp.route('/nurse')
@login_required
@nurse_viewer_required
def nurse_log():
    date_str  = request.args.get('date', '').strip()
    student_q = request.args.get('student', '').strip()

    q = _nurse_passes_query()
    day = None
    if date_str:
        try:
            day = datetime.strptime(date_str, '%Y-%m-%d').date()
            q = q.filter(db.func.date(Pass.time_out) == day)
        except ValueError:
            pass
    if student_q:
        q = q.join(Student).filter(db.or_(
            Student.first_name.ilike(f'%{student_q}%'),
            Student.last_name.ilike(f'%{student_q}%'),
        ))

    passes = q.order_by(Pass.time_out.desc()).all()
    return render_template('nurse_log.html',
        passes=passes, date_str=date_str, student_q=student_q)


@main_bp.route('/nurse/export.csv')
@login_required
@nurse_viewer_required
def nurse_export():
    from flask import Response
    date_str  = request.args.get('date', '').strip()
    student_q = request.args.get('student', '').strip()

    q = _nurse_passes_query()
    if date_str:
        try:
            day = datetime.strptime(date_str, '%Y-%m-%d').date()
            q = q.filter(db.func.date(Pass.time_out) == day)
        except ValueError:
            pass
    if student_q:
        q = q.join(Student).filter(db.or_(
            Student.first_name.ilike(f'%{student_q}%'),
            Student.last_name.ilike(f'%{student_q}%'),
        ))

    passes = q.order_by(Pass.time_out.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Date', 'Time Out', 'Time In', 'Duration (min)',
                     'Student', 'Grade', 'Period', 'Sending Teacher',
                     'Symptoms', 'Interventions', 'Nurse Notes'])
    for p in passes:
        extra = p.extra_data or {}
        writer.writerow([
            p.time_out.strftime('%Y-%m-%d') if p.time_out else '',
            p.time_out.strftime('%I:%M %p') if p.time_out else '',
            p.time_in.strftime('%I:%M %p')  if p.time_in  else '',
            p.duration_minutes if p.duration_minutes is not None else '',
            p.student.full_name,
            p.student.grade,
            p.period or '',
            p.teacher.name,
            '; '.join(extra.get('symptoms') or []),
            '; '.join(extra.get('interventions') or []),
            extra.get('nurse_notes', ''),
        ])

    filename = f"nurse_log_{date_str or 'all'}.csv"
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@main_bp.app_errorhandler(403)
def forbidden(e):
    return '<h2 style="font-family:sans-serif;text-align:center;margin-top:100px">403 — Not authorized</h2>', 403
