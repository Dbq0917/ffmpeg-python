"""Enforce the >=3 month retention policy for archived benchmark data.

Archive directories are named ``YYYYMMDDTHHMMSS`` under
``benchmarks/perf_archive``.  By default this runs as a dry run and only
reports what would be deleted; pass ``--apply`` to actually remove entries
older than the retention window.  The default window is 90 days but the
requirement is at least three months, so the value can only be lowered
deliberately by the operator.
"""

from __future__ import print_function

import argparse
import datetime
import os
import shutil

ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'perf_archive')
STAMP_FORMAT = '%Y%m%dT%H%M%S'


def find_expired(keep_days, now=None):
    now = now or datetime.datetime.now()
    expired, kept = [], []
    if not os.path.isdir(ARCHIVE_DIR):
        return expired, kept
    for name in sorted(os.listdir(ARCHIVE_DIR)):
        path = os.path.join(ARCHIVE_DIR, name)
        if not os.path.isdir(path):
            continue
        try:
            created = datetime.datetime.strptime(name, STAMP_FORMAT)
        except ValueError:
            # Never touch directories we did not create.
            kept.append((name, 'unrecognized-name'))
            continue
        age_days = (now - created).days
        if age_days > keep_days:
            expired.append((name, age_days))
        else:
            kept.append((name, age_days))
    return expired, kept


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--keep-days',
        type=int,
        default=90,
        help='Retention window in days (default 90 = ~3 months).',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Actually delete expired archives (default: dry run).',
    )
    args = parser.parse_args()
    if args.keep_days < 90:
        print(
            'WARNING: keep_days={} is shorter than the 3-month minimum.'.format(
                args.keep_days
            )
        )

    expired, kept = find_expired(args.keep_days)
    for name, age in expired:
        path = os.path.join(ARCHIVE_DIR, name)
        if args.apply:
            shutil.rmtree(path)
            print('deleted {} ({} days old)'.format(name, age))
        else:
            print('[dry-run] would delete {} ({} days old)'.format(name, age))
    print(
        'kept {} archive(s); expired {} (apply={}).'.format(
            len(kept), len(expired), args.apply
        )
    )


if __name__ == '__main__':
    main()
