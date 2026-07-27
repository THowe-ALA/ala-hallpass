"""READ-ONLY dry run for ALLOWED_TEACHERS. Runs SELECTs only.

Lists every account that already exists, and -- if you give it a proposed
allowlist -- shows exactly who would still be able to sign in and who would be
locked out. Run this BEFORE setting the variable on Railway.

Matters because auth.py checks the allowlist on EVERY login, not just at account
creation, so an existing account whose email is missing from the list loses
access the next time they sign in.

USAGE
    Set DATABASE_URL to the Railway Postgres DATABASE_PUBLIC_URL, then:

        python check_allowlist.py
            -> just list the accounts that exist today

        python check_allowlist.py "a@x.org, b@x.org"
            -> also show who passes / who gets locked out under that list
"""

import os
import sys

from sqlalchemy import create_engine, text


def get_engine():
    url = os.environ.get('DATABASE_URL')
    if not url:
        sys.exit('DATABASE_URL is not set. Use the Railway Postgres DATABASE_PUBLIC_URL.')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return create_engine(url)


def main():
    proposed_raw = sys.argv[1] if len(sys.argv) > 1 else ''
    proposed = {e.strip().lower() for e in proposed_raw.split(',') if e.strip()}
    admin_email = os.environ.get('ADMIN_EMAIL', 'theresa.howe@gmail.com').strip().lower()

    with get_engine().connect() as conn:
        users = conn.execute(text(
            'SELECT name, email, role FROM users ORDER BY role, name')).fetchall()

    print(f'=== Accounts that exist today ({len(users)}) ===')
    for u in users:
        print(f'  {u.role:8} {u.email:38} {u.name}')

    if not proposed:
        print('\n(No proposed list given. Re-run with the list in quotes to dry-run it:')
        print('   python check_allowlist.py "first@x.org, second@x.org" )')
        return

    print(f'\n=== Dry run against your proposed list ({len(proposed)} emails) ===')
    print(f'  ADMIN_EMAIL (always allowed regardless): {admin_email}')

    locked_out = []
    for u in users:
        email = (u.email or '').strip().lower()
        ok = email in proposed or email == admin_email
        why = ''
        if ok and email not in proposed:
            why = '  <- via ADMIN_EMAIL, not in the list'
        print(f'  {"KEEPS ACCESS" if ok else "LOCKED OUT  "}  {u.email}{why}')
        if not ok:
            locked_out.append(u.email)

    on_list_no_account = sorted(proposed - {(u.email or '').strip().lower() for u in users})
    if on_list_no_account:
        print('\n  On your list but no account yet (fine - they sign in and it is created):')
        for e in on_list_no_account:
            print(f'    {e}')

    print()
    if locked_out:
        print(f'  WARNING: {len(locked_out)} existing account(s) would lose access:')
        for e in locked_out:
            print(f'    {e}')
        print('  Add them to the list, or leave them out on purpose.')
    else:
        print('  No existing account would be locked out.')


if __name__ == '__main__':
    main()
