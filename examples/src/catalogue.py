"""Offline catalogue. Each state-changing call uses only this catalogue's database."""
from contextlib import closing

from gate import connect, digest
from representation import canonical, load_canonical

SCHEMA = """
CREATE TABLE journal(seq INTEGER PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE decisions(publication_id TEXT PRIMARY KEY,
                       ref TEXT NOT NULL UNIQUE, body TEXT NOT NULL);
CREATE TABLE listings(publication_id TEXT PRIMARY KEY REFERENCES decisions(publication_id),
                      ref TEXT NOT NULL UNIQUE REFERENCES decisions(ref), body TEXT NOT NULL);
"""


def initialize(path):
    with closing(connect(path)) as db:
        db.executescript(SCHEMA)


def seal(fields):
    return {**fields, 'ref': 'catalogue/' + fields['outcome'].lower() + '/' + digest(fields)}


def evidence_matches(receipt, request, delivery_key):
    if not isinstance(receipt, dict):
        return False
    fields = dict(receipt)
    ref = fields.pop('ref', None)
    outcome = fields.get('outcome')
    if outcome not in ('POSTED', 'BLOCKED') or canonical(fields.get('request')) != canonical(request):
        return False
    if fields.get('request_digest') != digest(request):
        return False
    if fields.get('delivery_key') != (delivery_key if outcome == 'BLOCKED' else None):
        return False
    if type(fields.get('seq')) is not int or fields['seq'] < 1:
        return False
    return fields.get('issuer') == 'offline-catalogue' and seal(fields)['ref'] == ref


def lookup(path, publication_id):
    with closing(connect(path)) as db:
        row = db.execute('SELECT body FROM decisions WHERE publication_id=?', (publication_id,)).fetchone()
        return load_canonical(row[0]) if row else None


def apply(path, operation, request, delivery_key=None):
    """The first trusted request or block binds the publication. Replays use this same writer."""
    if operation not in ('post', 'block'):
        raise ValueError('unknown catalogue operation')
    if (operation == 'block') != (delivery_key is not None):
        raise ValueError('block requires the gate delivery key')
    publication = request['publication_id']
    fingerprint = digest(request)
    with closing(connect(path)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        seq = db.execute('SELECT coalesce(max(seq),0)+1 FROM journal').fetchone()[0]
        old = db.execute('SELECT body FROM decisions WHERE publication_id=?', (publication,)).fetchone()
        receipt = load_canonical(old[0]) if old else None
        reason = None
        if request['payload_digest'] != digest(request['payload']):
            reason = 'PAYLOAD_DIGEST_MISMATCH'
        elif receipt and (receipt['request_digest'] != fingerprint or canonical(receipt['request']) != canonical(request)):
            reason = 'REQUEST_CONFLICT'
        elif (receipt and operation == 'block' and receipt['outcome'] == 'BLOCKED'
              and receipt['delivery_key'] != delivery_key):
            reason = 'BLOCK_KEY_CONFLICT'
        if reason:
            result = {'result':'CONFLICT', 'reason':reason, 'original_ref':receipt['ref'] if receipt else None}
        else:
            if receipt is None:
                outcome = 'POSTED' if operation == 'post' else 'BLOCKED'
                receipt = seal(dict(issuer='offline-catalogue', seq=seq, outcome=outcome,
                                    request=request, request_digest=fingerprint, delivery_key=delivery_key))
                db.execute('INSERT INTO decisions VALUES(?,?,?)',
                           (publication, receipt['ref'], canonical(receipt)))
                if operation == 'post':
                    db.execute('INSERT INTO listings VALUES(?,?,?)',
                               (publication, receipt['ref'], canonical(request['payload'])))
            result = {'result':receipt['outcome'], 'receipt':receipt}
        record = dict(seq=seq, operation=operation, publication_id=publication,
                      approval_revision=request['approval']['id'], request=request,
                      request_digest=fingerprint, delivery_key=delivery_key,
                      result=result['result'], original_ref=receipt['ref'] if receipt else None,
                      new_decision=not old and reason is None)
        if reason:
            record['reason'] = reason
        db.execute('INSERT INTO journal VALUES(?,?)', (seq, canonical(record)))
        db.commit()
    return result
