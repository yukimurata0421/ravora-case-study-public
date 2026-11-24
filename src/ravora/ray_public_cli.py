#!/usr/bin/env python3
"""Print Ray's public observation JSON (aggregates only) from a read-only SQLite snapshot.

The site validates this export before writing its public snapshot.
"""
import argparse
from pathlib import Path
import sqlite3
import sys
import time


from ravora.database import Database
from ravora.ray_public import dumps, project


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=Path('state/conversation.sqlite3'))
    args = parser.parse_args()
    try:
        with Database.existing(args.db, readonly=True).connect() as db:
            db.execute('BEGIN')
            value = project(db, time.time())
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        parser.exit(1, f'{type(exc).__name__}\n')
    print(dumps(value))


if __name__ == '__main__':
    main()
