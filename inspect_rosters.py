"""READ-ONLY report on roster data. Runs SELECTs only -- it cannot modify anything.

Answers three things:
  1. Did migrate_roster_pk.py actually land on this database?
  2. Which students sit on more than one roster?
  3. Which students sit on the SAME teacher's roster more than once -- the case
     that was impossible before 2026-07-26 (Dance 2 + Ballroom 1).

USAGE
    Set DATABASE_URL to the Railway Postgres DATABASE_PUBLIC_URL (same one used
    for the migration), then:

        python inspect_rosters.py            # full report
        python inspect_rosters.py --counts   # numbers only, no student names
"""

import os
import sys
from collections import defaultdict

from sqlalchemy import create_engine, inspect, text

INDEX_NAME = 'uq_teacher_student_period'


def get_engine():
    url = os.environ.get('DATABASE_URL')
    if not url:
        sys.exit('DATABASE_URL is not set. Use the Railway Postgres DATABASE_PUBLIC_URL.')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return create_engine(url)


def main():
    names_ok = '--counts' not in sys.argv
    engine   = get_engine()

    cols = {c['name'] for c in inspect(engine).get_columns('teacher_students')}
    pk   = inspect(engine).get_pk_constraint('teacher_students').get('constrained_columns') or []
    ix_sql = {
        'postgresql': "SELECT 1 FROM pg_indexes WHERE tablename='teacher_students' AND indexname=:n",
        'sqlite':     "SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n",
    }.get(engine.dialect.name)

    with engine.connect() as conn:
        has_ix = conn.execute(text(ix_sql), {'n': INDEX_NAME}).scalar() is not None if ix_sql else None
        rows = conn.execute(text('''
            SELECT s.id, s.first_name, s.last_name, s.grade, u.name AS teacher, ts.period
            FROM teacher_students ts
            JOIN students s ON s.id = ts.student_id
            JOIN users    u ON u.id = ts.teacher_id
            ORDER BY s.last_name, s.first_name, u.name, ts.period
        ''')).fetchall()

    print('=== Schema ===')
    print('  surrogate id column :', 'id' in cols)
    print('  primary key         :', pk)
    print(f'  unique index        : {has_ix}   ({INDEX_NAME})')
    migrated = ('id' in cols) and pk == ['id'] and has_ix
    print('  MIGRATED            :', 'YES' if migrated else 'NO -- run migrate_roster_pk.py')

    print('\n=== Totals ===')
    by_student = defaultdict(list)
    for r in rows:
        by_student[(r.id, f'{r.first_name} {r.last_name}', r.grade)].append((r.teacher, r.period))
    print(f'  roster rows         : {len(rows)}')
    print(f'  distinct students   : {len(by_student)}')

    multi_teacher = {k: v for k, v in by_student.items() if len({t for t, _ in v}) > 1}
    print(f'  students on >1 TEACHER roster : {len(multi_teacher)}')

    same_teacher = {}
    for k, v in by_student.items():
        counts = defaultdict(int)
        for t, _ in v:
            counts[t] += 1
        dupes = {t: n for t, n in counts.items() if n > 1}
        if dupes:
            same_teacher[k] = (v, dupes)
    print(f'  students twice with the SAME teacher : {len(same_teacher)}'
          '   <- newly possible')

    if not names_ok:
        return

    if multi_teacher:
        print('\n=== On more than one teacher\'s roster ===')
        for (sid, name, grade), pairs in sorted(multi_teacher.items(), key=lambda x: x[0][1]):
            print(f'  {name} (gr {grade}, id {sid})')
            for t, p in pairs:
                print(f'      {p or "no period set":18} | {t}')

    if same_teacher:
        print('\n=== Same teacher, multiple periods (the Dance 2 / Ballroom 1 case) ===')
        for (sid, name, grade), (pairs, dupes) in sorted(same_teacher.items(), key=lambda x: x[0][1]):
            print(f'  {name} (gr {grade}, id {sid}) -- {dupes}')
            for t, p in pairs:
                print(f'      {p or "no period set":18} | {t}')
    else:
        print('\n  (No student is on one teacher\'s roster twice yet. Expected -- the '
              'schema only just started allowing it.)')

    print('\n=== Rows per teacher ===')
    per_teacher = defaultdict(lambda: defaultdict(int))
    for r in rows:
        per_teacher[r.teacher][r.period or 'no period set'] += 1
    for teacher in sorted(per_teacher):
        total = sum(per_teacher[teacher].values())
        # Plain hyphen, not an em dash: the Windows console is cp1252 and
        # renders a stray em dash as a replacement character.
        print(f'  {teacher} - {total} row{"s" if total != 1 else ""}')
        for p in sorted(per_teacher[teacher]):
            print(f'      {p:18} {per_teacher[teacher][p]}')


if __name__ == '__main__':
    main()
