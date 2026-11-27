"""DS-7 mandate trial. All mutations use a single SQLite writer in exactly one store."""
from contextlib import closing
from gate import connect, digest, event_at, record
from representation import canonical, load_canonical
import trial_gate


def scope(request):
    return {k: request[k] for k in ('gate', 'org', 'action', 'target')}


def same(a, b):
    return canonical(a) == canonical(b)


def event_key(notice):
    return canonical([notice['issuer'], notice['gate_event']])


def bound_receipt(kind, seq, **fields):
    body = dict(issuer='offline-catalogue', kind=kind, seq=seq, **fields)
    return dict(body, ref='catalogue/' + kind + '/' + digest(body))


def sealed(r, kind):
    if not isinstance(r, dict) or r.get('kind') != kind or r.get('issuer') != 'offline-catalogue':
        return False
    body = dict(r)
    ref = body.pop('ref', None)
    return (type(body.get('seq')) is int and body['seq'] > 0
            and ref == 'catalogue/' + kind + '/' + digest(body))


def gate_issue(db, state, tick, actor, cmd, pub, payload):
    try:
        return trial_gate.issue(db, state, tick, actor, cmd, pub, payload)
    except ValueError as err:
        return record(db, state, tick, actor, cmd['action'], 'INTENT_REFUSED', str(err),
                      attempted_key=cmd['key'], publication_id=pub)


def gate_revoke(db, state, tick, actor, aid):
    a = state['actions'][aid]
    who = a['owner']
    if actor != state['registrar'] or not state['actors'][actor]['active']:
        raise ValueError('registrar required')
    if 'revocation' in a:
        return a['revocation']
    request = event_at(db, a['intent_seq'])['request']
    old = state['actors'][who]
    if not old['active'] or old['revision'] != request['approval']['owner_mandate']:
        raise ValueError('unexpected prior owner mandate')
    seq = db.execute('SELECT coalesce(max(seq),0)+1 FROM journal').fetchone()[0]
    n = dict(issuer=state['gate'], registrar=actor, subject=who, scope=scope(request),
             prior_revision=old['revision'], revision=old['revision']+1, active=False,
             gate_event=dict(gate=state['gate'], seq=seq))
    old.update(revision=n['revision'], active=False)
    a['revocation'] = n
    record(db, state, tick, actor, aid, 'LOCAL_REVOCATION', 'NOTICE_AVAILABLE', notice=n,
           notice_digest=digest(n), intent_seq=a['intent_seq'])
    return n


def matches_gate_records(request, n, decision, change):
    if not sealed(change, 'mandate') or not same(change.get('notice'), n):
        return False
    if change.get('notice_digest') != digest(n) or not sealed(decision, 'publication'):
        return False
    if not same(decision.get('request'), request) or decision.get('request_digest') != digest(request):
        return False
    checked = decision.get('checked_mandate')
    expected = dict(scope=scope(request), subject=request['owner'])
    if decision.get('result') == 'POSTED':
        expected.update(revision=request['approval']['owner_mandate'], active=True, change_ref=None)
        return (decision.get('reason') == 'ACTIVE_MANDATE' and same(checked, expected)
                and decision['seq'] < change['seq'])
    expected.update(revision=n['revision'], active=False, change_ref=change['ref'])
    return (decision.get('result') == 'REFUSED' and decision.get('reason') == 'SUPERSEDED_MANDATE'
            and same(checked, expected) and change['seq'] < decision['seq'])


def reconcile(db, state, tick, aid, decision, change):
    a = state['actions'][aid]
    if 'resolution_seq' in a:
        return event_at(db, a['resolution_seq'])
    intent = event_at(db, a['intent_seq'])
    req, n = intent['request'], a['revocation']
    presented_change = change
    if change is None:
        change = a.get('verified_change')
    elif (sealed(change, 'mandate') and same(change.get('notice'), n)
          and change.get('notice_digest') == digest(n)):
        a['verified_change'] = change
    if not matches_gate_records(req, n, decision, change):
        record(db, state, tick, state['registrar'], aid, 'LOOKUP_INCONCLUSIVE', 'MISSING_OR_MISMATCHED',
               intent_seq=intent['seq'], presented_decision=decision, presented_change=presented_change)
        return dict(result='PENDING', intent_seq=intent['seq'], publication_id=req['publication_id'])
    e = record(db, state, tick, state['registrar'], aid, 'RECONCILED', decision['result'],
               intent_seq=intent['seq'], gate_event=n['gate_event'], request_digest=digest(req),
               publication_id=req['publication_id'], approval_revision=req['approval']['id'],
               decision=decision, change=change, publication_ref=decision['ref'], revocation_ref=change['ref'])
    a.update(resolution_seq=e['seq'], status='COMMITTED' if decision['result']=='POSTED' else 'REFUSED')
    return e


SCHEMA = """
CREATE TABLE authority(id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE journal(seq INTEGER PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE changes(id TEXT PRIMARY KEY, ref TEXT NOT NULL UNIQUE, body TEXT NOT NULL);
CREATE TABLE decisions(id TEXT PRIMARY KEY, ref TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
                       UNIQUE(id,ref));
CREATE TABLE listings(id TEXT PRIMARY KEY, ref TEXT NOT NULL UNIQUE, body TEXT NOT NULL,
                      FOREIGN KEY(id,ref) REFERENCES decisions(id,ref));
"""


def init_catalogue(path, config):
    with closing(connect(path)) as db:
        db.executescript('BEGIN IMMEDIATE;'+SCHEMA)
        state = dict(scope=config['scope'],subject=config['subject'],revision=1,active=True,change_ref=None)
        db.execute('INSERT INTO authority VALUES(1,?,?)',(canonical(config),canonical(state)))
        db.commit()


def get(db, table, key):
    if table not in ('changes', 'decisions'):
        raise ValueError('invalid receipt table')
    row = db.execute(f'SELECT body FROM {table} WHERE id=?',(key,)).fetchone()
    return load_canonical(row[0]) if row else None


def change_mandate(db, seq, cfg, current, principal, n):
    # Transport principal is supplied independently of notice contents.
    if (principal != cfg['registrar'] or n['registrar'] != principal or n['issuer'] != cfg['scope']['gate']
            or n['gate_event']['gate'] != n['issuer'] or type(n['gate_event']['seq']) is not int
            or n['gate_event']['seq'] <= 0):
        return dict(result='REFUSED',reason='UNTRUSTED_NOTICE')
    key = event_key(n)
    old = get(db,'changes',key)
    if old:
        if same(n,old['notice']):
            return dict(result='REVOKED', receipt=old)
        return dict(result='CONFLICT',reason='NOTICE_ID_CONFLICT',original_ref=old['ref'])
    if not same(n['scope'],current['scope']) or n['subject'] != current['subject']:
        return dict(result='REFUSED',reason='MANDATE_SCOPE_MISMATCH')
    if (type(n['revision']) is not int or type(n['prior_revision']) is not int
            or n['prior_revision'] != current['revision'] or n['revision'] <= current['revision']):
        return dict(result='REFUSED',reason='STALE_REVISION')
    if n['active'] is not False or current['active'] is not True:
        return dict(result='REFUSED',reason='REVOCATION_REQUIRED')
    receipt = bound_receipt('mandate',seq,notice=n,notice_digest=digest(n))
    db.execute('INSERT INTO changes VALUES(?,?,?)',(key,receipt['ref'],canonical(receipt)))
    updated = dict(current,revision=n['revision'],active=False,change_ref=receipt['ref'])
    db.execute('UPDATE authority SET body=? WHERE id=1',(canonical(updated),))
    return dict(result='REVOKED',receipt=receipt)


def decide(db, seq, request):
    key = request['publication_id']
    old = get(db,'decisions',key)
    if old:
        if same(request,old['request']):
            return dict(result=old['result'],receipt=old)
        return dict(result='CONFLICT',reason='POST_ID_CONFLICT',original_ref=old['ref'])
    row = db.execute('SELECT body FROM authority WHERE id=1').fetchone()
    current = load_canonical(row[0]) if row else None
    if not current:
        reason = 'MANDATE_UNAVAILABLE'
    elif not same(scope(request),current['scope']) or request['owner'] != current['subject']:
        reason = 'MANDATE_SCOPE_MISMATCH'
    elif request['payload_digest'] != digest(request['payload']):
        reason = 'PAYLOAD_DIGEST_MISMATCH'
    elif not current['active'] or not same(current['revision'],request['approval']['owner_mandate']):
        reason = 'SUPERSEDED_MANDATE'
    else:
        reason = 'ACTIVE_MANDATE'
    result = 'POSTED' if reason == 'ACTIVE_MANDATE' else 'REFUSED'
    receipt = bound_receipt('publication',seq,result=result,reason=reason,request=request,
                            request_digest=digest(request),checked_mandate=current)
    db.execute('INSERT INTO decisions VALUES(?,?,?)',(key,receipt['ref'],canonical(receipt)))
    if result == 'POSTED':
        db.execute('INSERT INTO listings VALUES(?,?,?)',(key,receipt['ref'],canonical(request['payload'])))
    return dict(result=result,receipt=receipt)


def catalogue_write(path, op, args):
    with closing(connect(path)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        seq = db.execute('SELECT coalesce(max(seq),0)+1 FROM journal').fetchone()[0]
        if op == 'notice':
            cfg, state = map(load_canonical,db.execute('SELECT config,body FROM authority WHERE id=1').fetchone())
            reply = change_mandate(db,seq,cfg,state,*args)
        elif op == 'post':
            reply = decide(db,seq,*args)
        else:
            raise ValueError('unknown operation')
        db.execute('INSERT INTO journal VALUES(?,?)',(seq,canonical(dict(seq=seq,operation=op,args=args,reply=reply))))
        db.commit()
        return reply


def lookup(path, pub, notice):
    with closing(connect(path)) as db:
        db.execute('BEGIN')
        return [get(db,'decisions',pub),get(db,'changes',event_key(notice))]
