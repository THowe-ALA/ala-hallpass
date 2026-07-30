import csv
import io
import os
import re
import pytz
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError
from app import db
from models import User, Student, TeacherStudent, Pass, EmergencyCheckin, Config

HALLWAY_PASS_TYPES = ('restroom', 'late')
DEFAULT_HALLWAY_MAX = 25

main_bp = Blueprint('main', __name__)

TZ = pytz.timezone('America/Phoenix')

SYMPTOMS     = ['Headache', 'Dizziness', 'Bleeding', 'Stomachache',
                'Shortness of breath', 'Nausea', 'Fever']
INTERVENTIONS = ['Bandaid', 'Water', 'Put head down', 'Saltines', 'Mint', 'Ice pack']
PASS_TYPES   = [
    ('restroom',         'Restroom'),
    ('nurse',            'Nurse'),
    ('office',           'Office'),
    ('student_services', 'Student Services'),
    ('late',             'Late Departure'),
    ('teacher_visit',    'Going to Another Teacher'),
]
PASS_LABELS  = dict(PASS_TYPES)
STUDENT_SERVICES_STAFF = ['Grace Wood', 'Maizey Clark', 'Melissa Molina Garcia', 'Other']
PERIODS      = ['Zero Period', 'Leadership Period', '1st Period', '2nd Period',
                '3rd Period', '4th Period', '5th Period', '6th Period', '7th Period',
                'Outside School Hours']

# The clock can't tell 4th from 5th: lunch runs in three cohorts (A/B/C), so a
# student's 4th hour ends at a different time depending on which lunch they have.
# Passes are therefore still time-stamped with one combined mid-day label, while
# rosters use the split '4th Period' / '5th Period' labels above.
MIDDAY_BLOCK = '4th / Lunch / 5th'
# Roster labels retired from the picker but still sitting on old rows — kept so
# those students stay visible in filter chips instead of silently disappearing.
LEGACY_PERIODS = [MIDDAY_BLOCK]
# Roster periods that live inside the mid-day block, for the '__midday__' filter.
MIDDAY_ROSTER_PERIODS = ['4th Period', '5th Period', MIDDAY_BLOCK]


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


def _emergency_viewer_emails():
    raw = os.environ.get('EMERGENCY_VIEWERS', '')
    return {e.strip().lower() for e in raw.split(',') if e.strip()}


def is_emergency_viewer(user):
    if not user or not user.is_authenticated:
        return False
    if user.role == 'admin':
        return True
    return (user.email or '').strip().lower() in _emergency_viewer_emails()


def get_config(key, default=None):
    row = Config.query.get(key)
    return row.value if row else default


def set_config(key, value):
    row = Config.query.get(key)
    if row:
        row.value = str(value)
    else:
        db.session.add(Config(key=key, value=str(value)))
    db.session.commit()


def get_hallway_max():
    try:
        return int(get_config('hallway_max', DEFAULT_HALLWAY_MAX))
    except (TypeError, ValueError):
        return DEFAULT_HALLWAY_MAX


def count_students_in_hallway():
    return (Pass.query
            .filter(Pass.pass_type.in_(HALLWAY_PASS_TYPES))
            .filter(Pass.time_out.isnot(None))
            .filter(Pass.time_in.is_(None))
            .count())


# ── Annual grade advancement (auto each July + manual admin button) ──────────

_promo_done_year = {'value': None}  # process-local cache so we don't hit the DB every request


def _advance_all_grades():
    """Bump every student up one grade level, capped at 12 (grade 12 left unchanged).
    Graduating seniors are deleted separately. Returns the number advanced."""
    n = (Student.query
         .filter(Student.grade < 12)
         .update({Student.grade: Student.grade + 1}, synchronize_session=False))
    db.session.commit()
    return n


def maybe_auto_advance_grades():
    """Advance grades once per year, the first time the app is used in July.
    Guarded by a unique Config lock key so concurrent workers can't double-bump."""
    now = datetime.now(TZ)
    if now.month != 7:
        return
    if _promo_done_year['value'] == now.year:
        return
    lock_key = f'grades_promoted_{now.year}'
    if Config.query.get(lock_key) is not None:
        _promo_done_year['value'] = now.year
        return
    # Claim the year atomically — only the worker that inserts the lock proceeds.
    db.session.add(Config(key=lock_key, value=now.strftime('%Y-%m-%d %H:%M')))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        _promo_done_year['value'] = now.year
        return
    _advance_all_grades()
    set_config('grades_promoted_at', now.strftime('%Y-%m-%d %H:%M'))
    set_config('grades_promoted_year', str(now.year))
    _promo_done_year['value'] = now.year


@main_bp.before_app_request
def _auto_advance_grades_hook():
    try:
        maybe_auto_advance_grades()
    except Exception:
        db.session.rollback()


def emergency_viewer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_emergency_viewer(current_user):
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
        if m(11,4)  <= t < m(13,15): return MIDDAY_BLOCK
        if m(13,19) <= t < m(14,5):  return '6th Period'
        if m(14,9)  <= t < m(14,55): return '7th Period'
    else:
        if m(7,20)  <= t < m(8,10):  return 'Zero Period'
        if m(8,15)  <= t < m(9,7):   return '1st Period'
        if m(9,11)  <= t < m(10,0):  return '2nd Period'
        if m(10,4)  <= t < m(10,53): return '3rd Period'
        if m(10,53) <= t < m(13,9):  return MIDDAY_BLOCK
        if m(13,13) <= t < m(14,2):  return '6th Period'
        if m(14,6)  <= t < m(14,55): return '7th Period'
    return 'Outside School Hours'


def _period_chips_for_teacher(teacher_id):
    """Return [(value, label, count), ...] for filter chips on roster/dashboard pages.

    `value` is what goes in the ?period= query param: None for "All",
    '__none__' for the unassigned bucket, or the period name itself.
    Legacy labels get a chip too, so students on a retired period are still
    reachable (and printable) instead of only showing up under "All".
    """
    from sqlalchemy import func
    rows = (db.session.query(TeacherStudent.period, func.count(TeacherStudent.student_id))
            .filter(TeacherStudent.teacher_id == teacher_id)
            .group_by(TeacherStudent.period)
            .all())
    by_period = {p: c for p, c in rows}
    total = sum(by_period.values())
    chips = [(None, 'All', total)]
    for p in PERIODS + LEGACY_PERIODS:
        if by_period.get(p, 0) > 0:
            chips.append((p, p, by_period[p]))
    if by_period.get(None, 0) > 0:
        chips.append(('__none__', 'Unassigned', by_period[None]))
    return chips


def _apply_period_filter(query, period_arg):
    """Add a TeacherStudent.period filter to a roster query based on the ?period= param."""
    if period_arg == '__none__':
        return query.filter(TeacherStudent.period.is_(None))
    if period_arg == '__midday__':
        return query.filter(TeacherStudent.period.in_(MIDDAY_ROSTER_PERIODS))
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
    # distinct(): a student the teacher has in two periods matches the join
    # twice, and the dashboard is one row per student, not per class.
    q = (db.session.query(Student)
         .join(TeacherStudent, Student.id == TeacherStudent.student_id)
         .filter(TeacherStudent.teacher_id == current_user.id))
    q = _apply_period_filter(q, period_arg)
    roster = q.distinct().order_by(Student.last_name, Student.first_name).all()
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

    hallway_count = count_students_in_hallway()
    hallway_max   = get_hallway_max()
    return render_template('scan.html',
        student=student, open_pass=open_pass,
        recent_flag=recent_flag, frequent_flag=frequent_flag,
        same_period_flag=same_period_flag, current_period=current_period,
        today_count=today_count, duration_so_far=duration_so_far,
        pass_types=PASS_TYPES, symptoms=SYMPTOMS,
        interventions=INTERVENTIONS,
        student_services_staff=STUDENT_SERVICES_STAFF,
        hallway_count=hallway_count, hallway_max=hallway_max,
        now=now_local)


@main_bp.route('/log_out/<int:student_id>', methods=['POST'])
@login_required
def log_out(student_id):
    student   = Student.query.get_or_404(student_id)
    if student.is_blocked:
        flash(f'{student.full_name} is blocked from passes. Contact an administrator.')
        return redirect(url_for('main.scan', token=student.token))
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
    elif pass_type == 'student_services':
        choice = request.form.get('destination_staff', '').strip()
        if choice == 'Other':
            choice = request.form.get('destination_staff_other', '').strip() or 'Other'
        extra['destination_staff'] = choice

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
        grade_f = request.args.get('grade', '')
        sq = Student.query
        if grade_f:
            try:
                sq = sq.filter(Student.grade == int(grade_f))
            except ValueError:
                grade_f = ''
        all_students = sq.order_by(Student.grade, Student.last_name, Student.first_name).all()
        buddy_map, buddy_pairs = get_cross_teacher_buddies(datetime.now(TZ))

        # School-wide "currently out" lookup: student_id -> info on their open pass.
        now_utc = datetime.utcnow()
        open_map = {}
        open_passes = (Pass.query
                       .filter(Pass.time_in.is_(None), Pass.time_out.isnot(None))
                       .all())
        for p in open_passes:
            prev = open_map.get(p.student_id)
            if prev is not None and prev['time_out'] >= p.time_out:
                continue
            open_map[p.student_id] = {
                'label':    PASS_LABELS.get(p.pass_type, p.pass_type),
                'teacher':  p.teacher.name if p.teacher else '',
                'minutes':  int((now_utc - p.time_out).total_seconds() // 60) if p.time_out else None,
                'time_out': p.time_out,
            }
        out_now = sum(1 for s in all_students if s.id in open_map)

        return render_template('students.html', students=all_students, rows=None,
                               is_admin=True, view='all',
                               chips=None, active_period=None,
                               buddy_map=buddy_map, buddy_pairs=buddy_pairs,
                               grade_f=grade_f, open_map=open_map,
                               out_now=out_now, total_shown=len(all_students))

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

        # Scoped to the period: adding a student you already have in a DIFFERENT
        # period is a second class, not an edit. This used to overwrite
        # existing.period, which silently MOVED the student out of their first
        # class instead of adding the second.
        existing = TeacherStudent.query.filter_by(
            teacher_id=current_user.id, student_id=student.id, period=period
        ).first()
        if existing:
            flash(f'{student.full_name} is already on your roster for '
                  f'{period or "no period"}.')
            return redirect(url_for('main.students'))

        db.session.add(TeacherStudent(
            teacher_id=current_user.id, student_id=student.id, period=period))
        db.session.commit()
        flash(f'{student.full_name} added to your roster'
              f'{" for " + period if period else ""}.')
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


def _ingest_roster_csv(file, period, teacher_id):
    """Parse an uploaded roster CSV and attach its students to teacher_id's roster.
    Returns (results_dict, None) on success, or (None, error_message) on failure.
    Shared by the teacher self-upload and the admin assign-roster flows."""
    if period and period not in PERIODS:
        period = None

    try:
        raw = file.read().decode('utf-8-sig')  # handle Excel BOM
    except UnicodeDecodeError:
        return None, 'Could not read the file. Please save it as CSV (UTF-8) and try again.'

    reader = csv.DictReader(io.StringIO(raw))
    mapping, missing = _map_columns(reader.fieldnames)
    if missing:
        return None, f"CSV is missing a '{missing}' column. Expected headers: first_name, last_name, grade."

    # Pre-load existing students into a dict keyed by (first.lower, last.lower, grade).
    existing = {}
    for s in Student.query.all():
        existing[(s.first_name.strip().lower(), s.last_name.strip().lower(), s.grade)] = s

    # Pre-load this teacher's roster keyed by (student_id, period). Keyed by
    # student_id alone, uploading a second class's CSV silently skipped every
    # student the teacher already had in another period.
    on_roster = {
        (ts.student_id, ts.period) for ts in
        TeacherStudent.query.filter_by(teacher_id=teacher_id).all()
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

        if (student.id, period) in on_roster:
            already_on_roster += 1
        else:
            db.session.add(TeacherStudent(
                teacher_id=teacher_id, student_id=student.id, period=period
            ))
            on_roster.add((student.id, period))
            added_to_roster += 1

    db.session.commit()
    return {
        'created': created,
        'added_to_roster': added_to_roster,
        'skipped_existing': skipped_existing,
        'already_on_roster': already_on_roster,
        'period': period,
        'errors': errors,
    }, None


@main_bp.route('/students/upload', methods=['GET', 'POST'])
@login_required
def upload_students():
    if request.method == 'GET':
        return render_template('upload_students.html', periods=PERIODS)

    file = request.files.get('csv_file')
    if not file or file.filename == '':
        flash('Please choose a CSV file to upload.')
        return redirect(url_for('main.upload_students'))

    results, error = _ingest_roster_csv(file, request.form.get('period') or None, current_user.id)
    if error:
        flash(error)
        return redirect(url_for('main.upload_students'))
    return render_template('upload_students.html', periods=PERIODS, results=results)


@main_bp.route('/students/<int:student_id>/remove', methods=['POST'])
@login_required
def remove_student(student_id):
    """Remove one roster row — the period the teacher is looking at.

    A student can now sit on the same teacher's roster in several periods, so
    the row is identified by the period posted from the table (the row the
    Remove button belongs to). Their other classes are left alone.
    """
    period = request.form.get('period') or None
    q = TeacherStudent.query.filter_by(teacher_id=current_user.id,
                                       student_id=student_id)
    ts = q.filter(TeacherStudent.period.is_(None) if period is None
                  else TeacherStudent.period == period).first_or_404()
    db.session.delete(ts)
    db.session.commit()
    flash(f'Student removed from your roster'
          f'{" for " + period if period else ""}.')
    return redirect(request.referrer or url_for('main.students'))


@main_bp.route('/students/remove-period', methods=['POST'])
@login_required
def remove_period_from_roster():
    """Clear one period off this teacher's roster in one go, so a mis-bucketed
    class can be re-uploaded under the right period.

    Only the roster link is deleted — the students themselves, their QR tokens,
    and their pass history all survive, so re-uploading the same names re-attaches
    them to the very same printed QR codes."""
    period_arg = request.form.get('period') or None
    if not period_arg:
        flash('Pick a period first — this only clears one period at a time.')
        return redirect(url_for('main.students'))

    q = TeacherStudent.query.filter_by(teacher_id=current_user.id)
    if period_arg == '__none__':
        q = q.filter(TeacherStudent.period.is_(None))
    elif period_arg == '__midday__':
        q = q.filter(TeacherStudent.period.in_(MIDDAY_ROSTER_PERIODS))
    else:
        q = q.filter(TeacherStudent.period == period_arg)

    n = q.delete(synchronize_session=False)
    db.session.commit()
    label = {'__none__': 'Unassigned', '__midday__': 'Mid-day (4th + 5th)'}.get(period_arg, period_arg)
    flash(f'Removed {n} student{"s" if n != 1 else ""} from your roster ({label}). '
          'The students and their QR codes still exist — re-upload a CSV to put them back.')
    return redirect(url_for('main.students'))


def _print_teacher(teacher_arg):
    """Resolve ?teacher=<id> on the print pages into a User, or None.

    Admin-only: a regular teacher who passes the param (or edits the URL) still
    gets their own roster, never someone else's.
    """
    if current_user.role != 'admin' or not teacher_arg:
        return None
    try:
        return User.query.get(int(teacher_arg))
    except (TypeError, ValueError):
        return None


def _print_roster(period_arg, teacher=None):
    # An admin printing for a specific teacher (?teacher=<id>) gets that teacher's
    # roster — all their hours when no period is given. An admin with neither
    # gets the full-school sheet. Everyone else gets their own roster.
    if teacher is None and current_user.role == 'admin' and not period_arg:
        return Student.query.order_by(Student.last_name, Student.first_name).all()
    teacher_id = teacher.id if teacher else current_user.id
    q = (db.session.query(Student)
         .join(TeacherStudent, Student.id == TeacherStudent.student_id)
         .filter(TeacherStudent.teacher_id == teacher_id))
    q = _apply_period_filter(q, period_arg)
    # distinct(): without it a student in two of this teacher's periods gets two
    # identical QR cards on the same print sheet.
    return q.distinct().order_by(Student.last_name, Student.first_name).all()


@main_bp.route('/print')
@login_required
def print_cards():
    period_arg = request.args.get('period') or None
    teacher  = _print_teacher(request.args.get('teacher'))
    roster   = _print_roster(period_arg, teacher)
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    return render_template('print_cards.html', roster=roster, base_url=base_url,
                           filter_period=period_arg, for_teacher=teacher)


@main_bp.route('/print/stickers')
@login_required
def print_stickers():
    period_arg = request.args.get('period') or None
    teacher  = _print_teacher(request.args.get('teacher'))
    roster   = _print_roster(period_arg, teacher)
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    return render_template('print_stickers.html', roster=roster, base_url=base_url,
                           filter_period=period_arg, for_teacher=teacher)


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
        # Legacy labels included so passes stamped before the 4th/5th split
        # are still filterable in the log.
        periods=PERIODS + LEGACY_PERIODS, pass_labels=PASS_LABELS, all_users=all_users,
        hallway_max=get_hallway_max(), hallway_count=count_students_in_hallway(),
        grades_promoted_at=get_config('grades_promoted_at'),
        current_year=datetime.now(TZ).year,
        grades_advanced_this_year=(str(get_config('grades_promoted_year')) == str(datetime.now(TZ).year)))


@main_bp.route('/admin/assign-roster', methods=['GET', 'POST'])
@login_required
@admin_required
def assign_roster():
    teachers = User.query.order_by(User.name).all()
    if request.method == 'GET':
        return render_template('assign_roster.html', teachers=teachers, periods=PERIODS)

    try:
        teacher_id = int(request.form.get('teacher_id', ''))
    except (TypeError, ValueError):
        teacher_id = None
    teacher = User.query.get(teacher_id) if teacher_id else None
    if not teacher:
        flash('Please choose a valid teacher.')
        return redirect(url_for('main.assign_roster'))

    file = request.files.get('csv_file')
    if not file or file.filename == '':
        flash('Please choose a CSV file to upload.')
        return redirect(url_for('main.assign_roster'))

    results, error = _ingest_roster_csv(file, request.form.get('period') or None, teacher.id)
    if error:
        flash(error)
        return redirect(url_for('main.assign_roster'))
    return render_template('assign_roster.html', teachers=teachers, periods=PERIODS,
                           results=results, assigned_teacher=teacher)


@main_bp.route('/admin/print-rosters')
@login_required
@admin_required
def print_by_teacher():
    """Pick any teacher + hour and print their QR cards / ID stickers.

    Reuses _period_chips_for_teacher so the hours listed here are exactly the
    ones that teacher actually has students in (legacy labels included).
    """
    from sqlalchemy import func
    # DISTINCT student_id, not a row count: a student in two of this teacher's
    # hours has two roster rows but only prints one card, and this number sits
    # next to the "All hours" print button.
    totals = dict(db.session.query(TeacherStudent.teacher_id,
                                   func.count(func.distinct(TeacherStudent.student_id)))
                  .group_by(TeacherStudent.teacher_id).all())
    rows = []
    for t in User.query.order_by(User.name).all():
        chips = _period_chips_for_teacher(t.id)
        rows.append({
            'teacher': t,
            'total':   totals.get(t.id, 0),
            # chips[0] is the "All" entry; the rest are real periods (+ Unassigned).
            'periods': chips[1:],
        })
    return render_template('print_by_teacher.html', rows=rows)


@main_bp.route('/admin/hallway_max', methods=['POST'])
@login_required
@admin_required
def set_hallway_max():
    try:
        n = int(request.form.get('hallway_max', ''))
    except ValueError:
        flash('Hallway max must be a number.')
        return redirect(url_for('main.admin'))
    if n < 1 or n > 500:
        flash('Hallway max must be between 1 and 500.')
        return redirect(url_for('main.admin'))
    set_config('hallway_max', n)
    flash(f'Hallway cap set to {n}.')
    return redirect(url_for('main.admin'))


@main_bp.route('/admin/advance-grades', methods=['POST'])
@login_required
@admin_required
def advance_grades():
    now = datetime.now(TZ)
    n = _advance_all_grades()
    lock_key = f'grades_promoted_{now.year}'
    if Config.query.get(lock_key) is None:
        db.session.add(Config(key=lock_key, value=now.strftime('%Y-%m-%d %H:%M')))
        db.session.commit()
    set_config('grades_promoted_at', now.strftime('%Y-%m-%d %H:%M'))
    set_config('grades_promoted_year', str(now.year))
    _promo_done_year['value'] = now.year
    flash(f'Advanced {n} student{"s" if n != 1 else ""} up one grade level. Grade 12 was left unchanged.')
    return redirect(url_for('main.admin'))


@main_bp.route('/admin/promote/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def promote(user_id):
    user = User.query.get_or_404(user_id)
    user.role = 'admin'
    db.session.commit()
    flash(f'{user.name} is now an admin.')
    return redirect(url_for('main.admin'))


@main_bp.route('/admin/students/<int:student_id>/block', methods=['POST'])
@login_required
@admin_required
def block_student(student_id):
    student = Student.query.get_or_404(student_id)
    note = (request.form.get('block_note') or '').strip()
    student.is_blocked = True
    student.block_note = note[:500] if note else None
    db.session.commit()
    flash(f'{student.full_name} is now blocked from passes.')
    return redirect(url_for('main.students', view='all'))


@main_bp.route('/admin/students/<int:student_id>/unblock', methods=['POST'])
@login_required
@admin_required
def unblock_student(student_id):
    student = Student.query.get_or_404(student_id)
    student.is_blocked = False
    student.block_note = None
    db.session.commit()
    flash(f'{student.full_name} is unblocked.')
    return redirect(url_for('main.students', view='all'))


@main_bp.route('/admin/students/<int:student_id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_student(student_id):
    """Admin one-off fix for a student's name/grade (e.g. a CSV typo) without
    deleting and re-adding them — keeps their token, roster links, and history."""
    student = Student.query.get_or_404(student_id)
    if request.method == 'POST':
        fn = (request.form.get('first_name') or '').strip()
        ln = (request.form.get('last_name') or '').strip()
        try:
            grade = int(request.form.get('grade', ''))
        except (TypeError, ValueError):
            grade = None
        if not fn or not ln or grade is None or not (7 <= grade <= 12):
            flash('Please enter a first name, last name, and a grade from 7 to 12.')
            return redirect(url_for('main.edit_student', student_id=student.id))
        student.first_name = fn
        student.last_name  = ln
        student.grade      = grade
        db.session.commit()
        flash(f'Updated {student.full_name}.')
        return redirect(url_for('main.students', view='all'))
    return render_template('edit_student.html', student=student,
                           schedule=get_student_schedule(student.id),
                           base_url=os.environ.get('BASE_URL', request.host_url.rstrip('/')))


def get_student_schedule(student_id):
    """Every roster this student sits on: [(period_or_None, teacher), ...].

    Ordered like a class schedule (PERIODS order), with legacy/unknown labels
    and unassigned rows last. A student can appear on several teachers'
    rosters, each with its own period — that's one row per pairing.
    """
    rows = (db.session.query(TeacherStudent.period, User)
            .join(User, User.id == TeacherStudent.teacher_id)
            .filter(TeacherStudent.student_id == student_id)
            .all())

    def sort_key(row):
        period = row[0]
        if period in PERIODS:
            return (0, PERIODS.index(period), (row[1].name or '').lower())
        if period:                      # legacy label still on old rows
            return (1, 0, (row[1].name or '').lower())
        return (2, 0, (row[1].name or '').lower())   # unassigned period

    return sorted(rows, key=sort_key)


def _parse_student_ids(raw_list):
    ids = []
    for x in raw_list:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    return ids


@main_bp.route('/admin/students/delete-confirm', methods=['POST'])
@login_required
@admin_required
def delete_students_confirm():
    """Review screen — shows exactly which students (and how much history) will be
    permanently deleted before anything is removed."""
    ids = _parse_student_ids(request.form.getlist('student_ids'))
    students = (Student.query.filter(Student.id.in_(ids))
                .order_by(Student.last_name, Student.first_name).all()) if ids else []
    if not students:
        flash('No students selected to delete.')
        return redirect(url_for('main.students', view='all'))

    pass_count    = Pass.query.filter(Pass.student_id.in_(ids)).count()
    checkin_count = EmergencyCheckin.query.filter(EmergencyCheckin.student_id.in_(ids)).count()
    roster_count  = TeacherStudent.query.filter(TeacherStudent.student_id.in_(ids)).count()
    return render_template('confirm_delete_students.html',
        students=students, pass_count=pass_count,
        checkin_count=checkin_count, roster_count=roster_count)


@main_bp.route('/admin/students/delete', methods=['POST'])
@login_required
@admin_required
def delete_students():
    """Permanently delete the selected students and ALL their dependent rows
    (passes, emergency check-ins, roster links). Irreversible."""
    ids = _parse_student_ids(request.form.getlist('student_ids'))
    if not ids:
        flash('No students selected to delete.')
        return redirect(url_for('main.students', view='all'))

    n = Student.query.filter(Student.id.in_(ids)).count()
    # Delete children first — no ON DELETE CASCADE is defined, so Postgres would
    # otherwise reject the student delete on the foreign keys.
    Pass.query.filter(Pass.student_id.in_(ids)).delete(synchronize_session=False)
    EmergencyCheckin.query.filter(EmergencyCheckin.student_id.in_(ids)).delete(synchronize_session=False)
    TeacherStudent.query.filter(TeacherStudent.student_id.in_(ids)).delete(synchronize_session=False)
    Student.query.filter(Student.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()

    flash(f'Permanently deleted {n} student{"s" if n != 1 else ""} and all their history.')
    return redirect(url_for('main.students', view='all'))


@main_bp.route('/emergency/checkin/<int:student_id>', methods=['POST'])
@login_required
def emergency_checkin(student_id):
    student = Student.query.get_or_404(student_id)
    c = EmergencyCheckin(student_id=student.id, teacher_id=current_user.id)
    db.session.add(c)
    db.session.commit()

    if request.headers.get('X-Requested-With') == 'fetch':
        from flask import jsonify
        return jsonify({
            'ok': True,
            'checkin_id':  c.id,
            'student_id':  student.id,
            'teacher':     current_user.name,
            'checked_at':  pytz.utc.localize(c.created_at).astimezone(TZ).strftime('%I:%M %p'),
        })

    if request.form.get('return_to') == 'roster':
        return redirect(url_for('main.emergency_my', period=request.form.get('period') or None))

    return redirect(url_for('main.emergency_confirm', checkin_id=c.id))


@main_bp.route('/emergency/confirm/<int:checkin_id>')
@login_required
def emergency_confirm(checkin_id):
    c = EmergencyCheckin.query.get_or_404(checkin_id)
    now_local = datetime.now(TZ)
    return render_template('emergency_confirm.html', c=c, now=now_local)


def _accounted_for(window_minutes):
    """Return (accounted_rows, unaccounted_students).

    accounted_rows: list of dicts {student, teacher, checked_at_local, minutes_ago}
                    one row per student — their MOST RECENT check-in in the window.
    unaccounted_students: students that exist but have no check-in inside the window.
    """
    cutoff_utc = datetime.utcnow() - timedelta(minutes=window_minutes)
    rows = (EmergencyCheckin.query
            .filter(EmergencyCheckin.created_at >= cutoff_utc)
            .order_by(EmergencyCheckin.created_at.desc())
            .all())

    seen = {}                                  # student_id -> most recent EmergencyCheckin
    for r in rows:
        if r.student_id not in seen:
            seen[r.student_id] = r

    now_utc = datetime.utcnow()
    accounted = []
    for sid, r in seen.items():
        minutes_ago = int((now_utc - r.created_at).total_seconds() // 60)
        local_dt    = pytz.utc.localize(r.created_at).astimezone(TZ)
        accounted.append({
            'student':     r.student,
            'teacher':     r.teacher,
            'checked_at':  local_dt,
            'minutes_ago': minutes_ago,
        })
    accounted.sort(key=lambda d: d['student'].last_name.lower())

    accounted_ids = set(seen.keys())
    unaccounted_q = Student.query
    if accounted_ids:
        unaccounted_q = unaccounted_q.filter(~Student.id.in_(accounted_ids))
    unaccounted = unaccounted_q.order_by(Student.last_name, Student.first_name).all()
    return accounted, unaccounted


@main_bp.route('/emergency/my')
@login_required
def emergency_my():
    now_local = datetime.now(TZ)
    period_arg = request.args.get('period')
    if period_arg is None:                    # not specified → auto-detect
        detected = get_period(now_local)
        if detected == 'Outside School Hours':
            period_arg = '__all__'
        elif detected == MIDDAY_BLOCK:
            # The clock can't tell 4th from 5th (three lunch cohorts), so during a
            # drill in that block show 4th AND 5th together rather than nothing.
            period_arg = '__midday__'
        else:
            period_arg = detected

    try:
        window = int(request.args.get('window', 30))
    except ValueError:
        window = 30
    window = max(1, min(window, 720))

    q = (db.session.query(Student, TeacherStudent.period)
         .join(TeacherStudent, Student.id == TeacherStudent.student_id)
         .filter(TeacherStudent.teacher_id == current_user.id))
    if period_arg and period_arg != '__all__':
        q = _apply_period_filter(q, period_arg)

    roster = q.order_by(Student.last_name, Student.first_name).all()

    # One row per student. A student in two of this teacher's periods matches
    # the join twice — the mid-day filter covers both 4th and 5th, and '__all__'
    # covers everything — which during a drill would inflate the roll-call total
    # and give one kid two Secure buttons.
    seen, deduped = set(), []
    for s, p in roster:
        if s.id in seen:
            continue
        seen.add(s.id)
        deduped.append((s, p))
    roster = deduped

    cutoff_utc = datetime.utcnow() - timedelta(minutes=window)
    recent = (EmergencyCheckin.query
              .filter(EmergencyCheckin.created_at >= cutoff_utc)
              .order_by(EmergencyCheckin.created_at.desc())
              .all())
    latest_by_student = {}
    for r in recent:
        if r.student_id not in latest_by_student:
            latest_by_student[r.student_id] = r

    rows = []
    for s, p in roster:
        r = latest_by_student.get(s.id)
        secure_info = None
        if r:
            local_dt = pytz.utc.localize(r.created_at).astimezone(TZ)
            mins_ago = int((datetime.utcnow() - r.created_at).total_seconds() // 60)
            secure_info = {
                'teacher':     r.teacher.name,
                'is_mine':     (r.teacher_id == current_user.id),
                'checked_at':  local_dt.strftime('%I:%M %p'),
                'minutes_ago': mins_ago,
            }
        rows.append({'student': s, 'period': p, 'secure': secure_info})

    chips = _period_chips_for_teacher(current_user.id)
    chips_with_all = [('__all__', 'All my students',
                       sum(c[2] for c in chips if c[0] is not None and c[0] != '__none__'))]
    # Combined mid-day chip — the one the clock auto-selects during that block.
    midday_count = sum(c[2] for c in chips if c[0] in MIDDAY_ROSTER_PERIODS)
    if midday_count:
        chips_with_all.append(('__midday__', 'Mid-day (4th + 5th)', midday_count))
    chips_with_all.extend([c for c in chips if c[0] is not None])

    return render_template('emergency_my.html',
        rows=rows, chips=chips_with_all,
        active_period=period_arg, window=window, now=now_local)


@main_bp.route('/emergency/rollcall')
@login_required
@emergency_viewer_required
def emergency_rollcall():
    try:
        window = int(request.args.get('window', 30))
    except ValueError:
        window = 30
    window = max(1, min(window, 720))          # 1 min .. 12 hr

    accounted, unaccounted = _accounted_for(window)
    return render_template('emergency_rollcall.html',
        accounted=accounted, unaccounted=unaccounted,
        window=window, now=datetime.now(TZ))


@main_bp.route('/emergency/rollcall.csv')
@login_required
@emergency_viewer_required
def emergency_rollcall_csv():
    from flask import Response
    try:
        window = int(request.args.get('window', 30))
    except ValueError:
        window = 30
    window = max(1, min(window, 720))

    accounted, unaccounted = _accounted_for(window)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Status', 'Student', 'Grade',
                     'Last Seen With (Adult)', 'Checked In At', 'Minutes Ago'])
    for a in accounted:
        writer.writerow(['Secure', a['student'].full_name, a['student'].grade,
                         a['teacher'].name,
                         a['checked_at'].strftime('%Y-%m-%d %I:%M %p'),
                         a['minutes_ago']])
    for s in unaccounted:
        writer.writerow(['Unaccounted', s.full_name, s.grade, '', '', ''])

    ts = datetime.now(TZ).strftime('%Y-%m-%d_%H%M')
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="rollcall_{ts}.csv"'},
    )


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
