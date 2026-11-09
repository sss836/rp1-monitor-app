"""Append-only local report audit. No network, equipment, or production DB access."""

import getpass
import json
from datetime import datetime, timezone

from . import __version__


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS report_source_history (
            sequence INTEGER PRIMARY KEY, sn TEXT NOT NULL, row_key TEXT NOT NULL,
            source_token TEXT NOT NULL, record_id TEXT NOT NULL,
            previous_record_id TEXT, observed_at TEXT NOT NULL, snapshot TEXT NOT NULL,
            UNIQUE(sn,row_key,source_token));
        CREATE TABLE IF NOT EXISTS report_edit_history (
            sequence INTEGER PRIMARY KEY, sn TEXT NOT NULL, row_key TEXT NOT NULL,
            source_token TEXT NOT NULL, changed_at TEXT NOT NULL,
            actor TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT NOT NULL,
            after_json TEXT NOT NULL, origin TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS report_edit_history_row
            ON report_edit_history(sn,row_key,sequence);
        CREATE TRIGGER IF NOT EXISTS report_source_no_update BEFORE UPDATE ON report_source_history
            BEGIN SELECT RAISE(ABORT,'immutable report source'); END;
        CREATE TRIGGER IF NOT EXISTS report_source_no_delete BEFORE DELETE ON report_source_history
            BEGIN SELECT RAISE(ABORT,'immutable report source'); END;
        CREATE TRIGGER IF NOT EXISTS report_edit_no_update BEFORE UPDATE ON report_edit_history
            BEGIN SELECT RAISE(ABORT,'immutable report history'); END;
        CREATE TRIGGER IF NOT EXISTS report_edit_no_delete BEFORE DELETE ON report_edit_history
            BEGIN SELECT RAISE(ABORT,'immutable report history'); END;
    """)
    # Preserve legacy effective edits without inventing their original author/time.
    with db:
        db.execute("""INSERT INTO report_edit_history
            (sn,row_key,source_token,changed_at,actor,reason,before_json,after_json,origin)
            SELECT e.sn,e.row_key,e.source_token,?,'unknown:legacy',
                '升级时保留既有编辑；原修改人和时间未知','{}',e.fields,'legacy_import'
            FROM report_edits e WHERE NOT EXISTS (
                SELECT 1 FROM report_edit_history h WHERE h.sn=e.sn AND h.row_key=e.row_key)""", (now(),))


def remember_source(db, sn, row):
    previous = db.execute("""SELECT record_id FROM report_source_history
        WHERE sn=? AND row_key=? ORDER BY sequence DESC LIMIT 1""", (sn, row['row_key'])).fetchone()
    db.execute("""INSERT OR IGNORE INTO report_source_history
        (sn,row_key,source_token,record_id,previous_record_id,observed_at,snapshot)
        VALUES(?,?,?,?,?,?,?)""", (sn, row['row_key'], row['source_token'], row['record_id'],
                                   previous[0] if previous else None, now(), encoded(row)))


def edit_token(db, sn, key):
    return db.execute('SELECT COALESCE(MAX(sequence),0) FROM report_edit_history WHERE sn=? AND row_key=?',
                      (sn, key)).fetchone()[0]


def append_edit(db, sn, key, source_token, before, after, reason):
    db.execute("""INSERT INTO report_edit_history
        (sn,row_key,source_token,changed_at,actor,reason,before_json,after_json,origin)
        VALUES(?,?,?,?,?,?,?,?,?)""", (sn, key, source_token, now(),
        'local-os:' + getpass.getuser(), reason, encoded(before), encoded(after), 'local_edit'))


def manifest(db, sn, rows, csv_sha256, rules_sha256):
    sources = [dict(row) for row in db.execute(
        'SELECT * FROM report_source_history WHERE sn=? ORDER BY sequence', (sn,))]
    edits = [dict(row) for row in db.execute(
        'SELECT * FROM report_edit_history WHERE sn=? ORDER BY sequence', (sn,))]
    for row in sources:
        row['snapshot'] = json.loads(row['snapshot'])
    for row in edits:
        row['before'] = json.loads(row.pop('before_json'))
        row['after'] = json.loads(row.pop('after_json'))
    # A repeated export of the same CSV/evidence must retain identical bytes:
    # central archives bind one immutable trace hash to the CSV hash.
    snapshot_at = max([row['observed_at'] for row in sources]
                      + [row['changed_at'] for row in edits], default='1970-01-01T00:00:00+00:00')
    return {'schema': 'rp1.summary-trace.v1', 'sample_id': sn, 'csv_sha256': csv_sha256,
            'created_at': snapshot_at, 'software_version': __version__,
            'rules_sha256': rules_sha256, 'rows': rows, 'source_history': sources, 'edit_history': edits,
            'identity_scope': 'local OS account, not central authenticated reviewer',
            'history_scope': 'summary snapshots since upgrade; older original sources remain in local store',
            'formal_statistics_eligible': False}
