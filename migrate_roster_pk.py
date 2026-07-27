"""One-time migration: let a student sit on the SAME teacher's roster in more
than one period.

WHY
    teacher_students PK is (teacher_id, student_id), so a teacher who has the
    same kid for two subjects -- e.g. Hamburg with Lizzy in both Dance 2 and
    Ballroom 1 -- can only record one of them. Today a CSV upload silently
    skips the second class, and "+ Add Student" silently MOVES the student out
    of the first one.

WHAT IT DOES
    before:  PRIMARY KEY (teacher_id, student_id)
    after:   surrogate integer "id" PRIMARY KEY
             + UNIQUE (teacher_id, student_id, COALESCE(period, ''))

    The COALESCE is deliberate. A plain UNIQUE over a nullable column treats
    every NULL as distinct, so "no period set" rows could silently duplicate.
    Folding NULL to '' keeps NULL meaning "unassigned" everywhere in the app
    (routes.py filters on period IS NULL) while still deduping those rows.

ORDERING
    Safe to run BEFORE deploying the matching code change. The new schema only
    LOOSENS a constraint, and the app dedupes in Python (routes.py:598) rather
    than leaning on the DB, so the currently-deployed code keeps working
    unchanged against the migrated table.

USAGE
    Set DATABASE_URL to the Railway Postgres *public* connection string
    (Railway -> Postgres service -> Variables -> DATABASE_PUBLIC_URL), then:

        python migrate_roster_pk.py            # dry run -- changes nothing
        python migrate_roster_pk.py --apply    # perform the migration

    A CSV backup of teacher_students is written next to this script before any
    DDL runs. Re-running after a successful migration is a no-op.
"""

import csv
import os
import sys
from datetime import datetime

from sqlalchemy import create_engine, inspect, text

INDEX_NAME = 'uq_teacher_student_period'


def get_engine():
    url = os.environ.get('DATABASE_URL')
    if not url:
        sys.exit('DATABASE_URL is not set.\n'
                 'Use the Railway Postgres DATABASE_PUBLIC_URL for a machine '
                 'outside Railway (the internal .railway.internal host only '
                 'resolves from inside Railway).')
    if url.startswith('postgres://'):           # Railway hands out postgres://
        url = url.replace('postgres://', 'postgresql://', 1)
    return create_engine(url)


def already_migrated(engine):
    cols = {c['name'] for c in inspect(engine).get_columns('teacher_students')}
    return 'id' in cols


def backup(engine):
    """Dump teacher_students to CSV before touching anything."""
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    path  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         f'roster_backup_{stamp}.csv')
    with engine.connect() as conn:
        rows = conn.execute(text(
            'SELECT teacher_id, student_id, period FROM teacher_students '
            'ORDER BY teacher_id, student_id')).fetchall()
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['teacher_id', 'student_id', 'period'])
        w.writerows(rows)
    return path, len(rows)


def find_blocking_duplicates(engine):
    """Rows that would violate the new unique index.

    Should always be empty: the old PK (teacher_id, student_id) is STRICTER
    than the new constraint, so anything that fits today also fits after. This
    checks anyway rather than discovering it half way through the DDL.
    """
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT teacher_id, student_id, COALESCE(period, '') AS p, COUNT(*) AS n "
            'FROM teacher_students '
            "GROUP BY teacher_id, student_id, COALESCE(period, '') "
            'HAVING COUNT(*) > 1')).fetchall()


def migrate_postgres(conn):
    pk = conn.execute(text(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'teacher_students'::regclass AND contype = 'p'"
    )).scalar()
    if pk:
        # Constraint name is looked up rather than assumed -- it is usually
        # teacher_students_pkey, but not guaranteed.
        conn.execute(text(f'ALTER TABLE teacher_students DROP CONSTRAINT "{pk}"'))
    conn.execute(text('ALTER TABLE teacher_students ADD COLUMN id SERIAL PRIMARY KEY'))
    conn.execute(text(
        f'CREATE UNIQUE INDEX {INDEX_NAME} ON teacher_students '
        "(teacher_id, student_id, COALESCE(period, ''))"))


def migrate_sqlite(conn):
    """SQLite can't ALTER a primary key, so the table gets rebuilt.

    Only used for local testing -- production is Postgres.
    """
    conn.execute(text('''
        CREATE TABLE teacher_students_new (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id INTEGER NOT NULL REFERENCES users(id),
            student_id INTEGER NOT NULL REFERENCES students(id),
            period     VARCHAR(50)
        )'''))
    conn.execute(text(
        'INSERT INTO teacher_students_new (teacher_id, student_id, period) '
        'SELECT teacher_id, student_id, period FROM teacher_students'))
    conn.execute(text('DROP TABLE teacher_students'))
    conn.execute(text('ALTER TABLE teacher_students_new RENAME TO teacher_students'))
    conn.execute(text(
        f'CREATE UNIQUE INDEX {INDEX_NAME} ON teacher_students '
        "(teacher_id, student_id, COALESCE(period, ''))"))


def has_unique_index(engine, conn=None):
    """Look the index up in the DB catalog directly.

    SQLAlchemy's get_indexes() does NOT report expression indexes, so asking it
    about a COALESCE(...) index returns a false negative -- it says the index is
    missing while the database is happily enforcing it.
    """
    sql = {
        'postgresql': "SELECT 1 FROM pg_indexes WHERE tablename = 'teacher_students' "
                      'AND indexname = :n',
        'sqlite':     "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = :n",
    }.get(engine.dialect.name)
    if sql is None:
        return None                     # unknown dialect: report "can't tell"
    runner = conn if conn is not None else engine.connect()
    try:
        return runner.execute(text(sql), {'n': INDEX_NAME}).scalar() is not None
    finally:
        if conn is None:
            runner.close()


def verify(engine):
    insp = inspect(engine)
    cols = {c['name'] for c in insp.get_columns('teacher_students')}
    pk   = insp.get_pk_constraint('teacher_students').get('constrained_columns') or []
    with engine.connect() as conn:
        n = conn.execute(text('SELECT COUNT(*) FROM teacher_students')).scalar()
    return {
        'has_id_column': 'id' in cols,
        'pk_columns':    pk,
        'has_unique_ix': has_unique_index(engine),
        'row_count':     n,
    }


def main():
    apply_it = '--apply' in sys.argv
    engine   = get_engine()
    dialect  = engine.dialect.name
    print(f'Database: {dialect}')

    if already_migrated(engine):
        print('Already migrated -- teacher_students has an "id" column. Nothing to do.')
        print('Current state:', verify(engine))
        return

    dupes = find_blocking_duplicates(engine)
    if dupes:
        print('ABORT: rows that would violate the new unique constraint:')
        for d in dupes:
            print('   teacher_id=%s student_id=%s period=%r count=%s' % tuple(d))
        sys.exit(1)

    with engine.connect() as conn:
        before = conn.execute(text('SELECT COUNT(*) FROM teacher_students')).scalar()
    print(f'teacher_students rows: {before}')
    print('No blocking duplicates.')

    if not apply_it:
        print('\nDRY RUN -- nothing changed. Planned:')
        print('  1. back up teacher_students to CSV')
        print('  2. drop PRIMARY KEY (teacher_id, student_id)')
        print('  3. add surrogate "id" primary key')
        print(f'  4. create unique index {INDEX_NAME} '
              "on (teacher_id, student_id, COALESCE(period, ''))")
        print('\nRe-run with --apply to perform it.')
        return

    path, n = backup(engine)
    print(f'Backed up {n} rows -> {path}')

    # One transaction: a failure part way through rolls the table back rather
    # than leaving it with no primary key.
    with engine.begin() as conn:
        if dialect == 'postgresql':
            migrate_postgres(conn)
        elif dialect == 'sqlite':
            migrate_sqlite(conn)
        else:
            sys.exit(f'Unsupported database dialect: {dialect}')

    state = verify(engine)
    print('Migration applied. Verified state:', state)
    if state['row_count'] != before:
        sys.exit(f'ROW COUNT CHANGED: {before} -> {state["row_count"]}. '
                 f'Restore from {path}.')
    if not state['has_id_column'] or state['pk_columns'] != ['id']:
        sys.exit(f'PRIMARY KEY not as expected. Restore from {path}.')
    if state['has_unique_ix'] is False:
        sys.exit(f'Unique index {INDEX_NAME} missing -- duplicate roster rows '
                 f'would be possible. Restore from {path}.')
    print(f'Row count unchanged ({before}).')
    print('\nNext: deploy the matching code change (models.py + the roster '
          'paths in routes.py). Until then the app behaves exactly as before.')


if __name__ == '__main__':
    main()
