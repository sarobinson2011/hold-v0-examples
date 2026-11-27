"""Gate-side DS-7 trial adapter. Trusted ticks and principals arrive outside the commands."""
from contextlib import closing

from catalogue import evidence_matches
from gate import active, advance, approval_error, connect, digest, event_at, open_cases, record
from messages import valid
from representation import canonical, load_canonical


def write(path, tick, handler, *args):
    with closing(connect(path)) as db:
        db.execute('BEGIN IMMEDIATE')
        state = load_canonical(db.execute('SELECT body FROM local_state WHERE id=1').fetchone()[0])
        advance(db, state, tick)
        response = handler(db, state, tick, *args)
        db.execute('UPDATE local_state SET body=? WHERE id=1', (canonical(state),))
        db.commit()
        return response


def issue(db, state, tick, owner, cmd, publication_id, payload):
    if not valid(cmd) or cmd['op'] != 'commit':
        raise ValueError('invalid fixture posting command')
    aid = cmd['action']
    a = state['actions'][aid]
    p = a['approval']
    if (owner != a['owner'] or a['status'] != 'APPROVED' or open_cases(a)
        or cmd['target'] != a['target'] or cmd['approval_id'] != p['id']):
        raise ValueError('posting authority, status, target or revision mismatch')
    error = approval_error(state, a, p, {a['reviewer']})
    if error:
        raise ValueError(error)
    if 'intent_seq' in a:
        raise ValueError('publication intent already issued')
    request = dict(publication_id=publication_id, gate=state['gate'], org=state['org'], action=aid,
                   target=a['target'], owner=owner, approval=p, approval_seq=a['approval_seq'],
                   command_key=cmd['key'], payload=payload, payload_digest=digest(payload))
    intent = record(db,state,tick,owner,aid,'POST_INTENT','ISSUED',request=request,request_digest=digest(request))
    a['intent_seq'] = intent['seq']
    return request


def eligibility(state, actor, cmd, basis):
    a = state['actions'][cmd['action']]
    core = cmd['challenge']
    if (actor != core['challenger'] or not active(state,actor)
        or state['actors'][actor]['role'] not in a['roles'] or cmd['org'] != state['org']):
        return 'IDENTITY_ROLE_OR_SCOPE_MISMATCH'
    if core['claim'] != basis['claim']:
        return 'RELIANCE_MISMATCH'
    if core['ground'] not in a['grounds']:
        return 'GROUND_NOT_ALLOWED'
    if a['routes'].get(cmd['ref']) != a['reviewer'] or not active(state,a['reviewer']):
        return 'REVIEWER_ROUTE_UNAVAILABLE'
    return None


def intake(db, state, tick, actor, cmd):
    if actor not in state['actors'] or not valid(cmd) or cmd['op'] != 'deliver':
        raise ValueError('trial intake requires a trusted principal and a valid delivery')
    aid = cmd['action']
    a = state['actions'][aid]
    core = cmd['challenge']
    key = [actor, core['id'], cmd['org'], aid]
    entry = state['deliveries'].get(canonical(key))
    fingerprint = digest({k:v for k,v in cmd.items() if k != 'sent_tick'})
    if entry:
        if entry['digest'] != fingerprint:
            return record(db,state,tick,actor,aid,'REJECTED','DELIVERY_KEY_CONFLICT',prior=entry['seq'])
        return event_at(db,entry['seq'])
    req = event_at(db,a['intent_seq'])['request']
    error = eligibility(state,actor,cmd,req['approval'])
    result = 'REJECTED' if error else 'PENDING'
    e = record(db,state,tick,actor,aid,result,error or 'ELIGIBLE_AWAITING_CATALOGUE',
               delivery_key=key, delivery=cmd, delivery_digest=fingerprint, reviewer=a['reviewer'],
               intent_seq=a['intent_seq'], publication_id=req['publication_id'],
               approval_revision=req['approval']['id'], request_digest=digest(req))
    state['deliveries'][canonical(key)] = dict(seq=e['seq'],digest=fingerprint)
    return e


def block_message(path, pending_seq):
    """Read an already committed intake and intent. No catalogue access is possible here."""
    with closing(connect(path)) as db:
        e = event_at(db,pending_seq)
        if e['result'] != 'PENDING':
            raise ValueError('eligible pending intake required')
        return dict(request=event_at(db,e['intent_seq'])['request'], delivery_key=e['delivery_key'])


def reconcile(db, state, tick, pending_seq, reply):
    e = event_at(db,pending_seq)
    if e['result'] != 'PENDING':
        raise ValueError('pending intake reference required')
    aid = e['action']
    a = state['actions'][aid]
    key = e['delivery_key']
    current = state['deliveries'][canonical(key)]
    if current['seq'] != pending_seq:
        return event_at(db,current['seq'])
    request = event_at(db,e['intent_seq'])['request']
    common = dict(pending_seq=pending_seq, intake_tick=e['tick'], publication_id=request['publication_id'],
                  approval_revision=request['approval']['id'], intent_seq=e['intent_seq'],
                  request_digest=digest(request), delivery_key=key)
    if not evidence_matches(reply,request,key):
        record(db,state,tick,state['registrar'],aid,'LOOKUP_INCONCLUSIVE',
               'LOOKUP_UNAVAILABLE' if reply is None else 'EVIDENCE_MISMATCH',
               presented=reply, **common)
        return e
    cmd = e['delivery']
    # For LATE the source of this basis is the stored catalogue posting receipt.
    basis = reply['request']['approval']
    error = eligibility(state,e['actor'],cmd,basis)
    detail = dict(**common,catalogue_ref=reply['ref'],catalogue_receipt=reply,checked_basis=basis,
                  challenge=cmd['challenge']['id'],ground=cmd['challenge']['ground'],
                  private_ref=cmd['ref'], reviewer=a['reviewer'])
    if error:
        result, reason = 'REJECTED', error
    elif reply['outcome'] == 'BLOCKED':
        result, reason = 'ADMITTED', 'CATALOGUE_BLOCK_VERIFIED'
        detail.update(due=tick+a['period'],escalation_reviewer=a['lead'],cost_bearer=state['org'],
                      cost_items=a['cost_items'])
    else:
        result, reason = 'LATE', 'CATALOGUE_POST_VERIFIED'
        detail['commitment'] = reply['ref']
    receipt = record(db,state,tick,e['actor'],aid,result,reason,**detail)
    current['seq'] = receipt['seq']
    a['catalogue_ref'] = reply['ref']
    if reply['outcome'] == 'POSTED':
        a['status'] = 'COMMITTED'
    if result == 'ADMITTED':
        seq = receipt['seq']
        a['cases'][str(seq)] = dict(status='OPEN',assignment=seq,challenge=detail['challenge'],
                                    challenger=e['actor'],reviewer=a['reviewer'],due=detail['due'],
                                    overdue=None,petitions=[],ref=cmd['ref'])
        a['status'] = 'HELD'
    return receipt
