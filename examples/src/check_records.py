"""Reconstruct the four criteria from read-only records without importing the gate or catalogue."""
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def read(path):
    return json.loads(path.read_bytes())


def digest(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,separators=(',',':'),
                                     ensure_ascii=False).encode()).hexdigest()


def require(condition, record):
    if not condition:
        raise AssertionError('contrary durable record: '+record)


def rows(db, sql):
    return [json.loads(row[0]) for row in db.execute(sql)]


def inspect_history(folder, expected, req, notice):
    name = folder.name
    outcomes = []
    for phase in ('restart','final'):
        prefix = 'restart-' if phase=='restart' else ''
        def open_store(stem):
            return closing(sqlite3.connect((folder/(prefix+stem+'.sqlite')).resolve().as_uri()+'?mode=ro',uri=True))
        with open_store('gate') as g, open_store('catalogue') as c:
            gj = rows(g,'SELECT body FROM journal ORDER BY seq')
            st = rows(g,'SELECT body FROM local_state')[0]
            a = st['actions'][req['action']]
            decisions = rows(c,'SELECT body FROM decisions')
            changes = rows(c,'SELECT body FROM changes')
            cj = rows(c,'SELECT body FROM journal ORDER BY seq')
            listings = c.execute('SELECT id,ref,body FROM listings ORDER BY id').fetchall()
            auth = rows(c,'SELECT body FROM authority')[0]
        loc = name+'/'+phase
        require(gj[1]['request']==req and gj[1]['request_digest']==digest(req),loc+' intent')
        require(gj[2]['notice']==notice==a['revocation'] and notice['gate_event']['seq']==gj[2]['seq'],loc+' notice')
        require(st['actors'][notice['subject']]['revision']==2 and not st['actors'][notice['subject']]['active']
                and st['actors']['Ellis Chen']['active'] and st['actors']['Ellis Chen']['revision']==1,loc+' mandates')
        require(a['approval']==req['approval'] and a['current']['claim']==req['approval']['claim']
                and a['cases']=={},loc+' approval or holds')
        if phase=='restart':
            require(len(gj)==3 and 'resolution_seq' not in a,loc+' pending')
            require([auth['revision'],auth['active'],len(listings),len(decisions)]==expected['cut'],loc+' catalogue cut')
            require(len(cj)==1 and len(changes)+len(decisions)==1,loc+' lost-reply receipt')
            first = (changes+decisions)[0]
            require(first==cj[0]['reply']['receipt'],loc+' first reference')
        else:
            require(len(decisions)==len(changes)==1,loc+' primary receipt counts')
            d,m = decisions[0],changes[0]
            require(d['request']==req and d['request_digest']==digest(req) and m['notice']==notice
                    and m['notice_digest']==digest(notice),loc+' receipt joins')
            require([m['seq'],d['seq'],d['result']]==[expected['mandate_seq'],expected['decision_seq'],expected['outcome']],
                    loc+' catalogue sequences')
            require(cj[d['seq']-1]['reply']['receipt']==d and cj[m['seq']-1]['reply']['receipt']==m
                    and (m if m['seq']==1 else d)==first,loc+' recovered first receipts')
            require(len(listings)==expected['visible'] and all(pub==req['publication_id'] and ref==d['ref']
                    and json.loads(body)==req['payload'] for pub,ref,body in listings),loc+' listing join')
            require(auth['active'] is False and auth['revision']==2 and auth['change_ref']==m['ref'],loc+' catalogue authority')
            require(gj[-1]['seq']==a['resolution_seq']==9 and gj[-1]['decision']==d and gj[-1]['change']==m
                    and gj[-1]['intent_seq']==2 and gj[-1]['gate_event']==notice['gate_event'],loc+' gate final binding')
            require(d['checked_mandate']['change_ref']==(m['ref'] if d['result']=='REFUSED' else None),loc+' authority at effect')
            require([r['reply']['result'] for r in cj[2:]]==[d['result'],'REVOKED','CONFLICT','CONFLICT','REFUSED']
                    and cj[2]['reply']['receipt']==d and cj[3]['reply']['receipt']==m,loc+' controls')
        outcomes.append(dict(phase=phase,gate_sequence=gj[-1]['seq'],catalogue_sequence=cj[-1]['seq'],
                             visible=len(listings),gate_intent=a['intent_seq'],catalogue_revision=auth['revision']))
    transcript = read(folder/'transcript.json')
    ops = []
    for e in transcript:
        if e.get('snapshot')=='restart':
            break
        if e['op']=='dispatch':
            ops.append('dispatch')
        if e.get('store')=='gate' and e['op']=='gate_revoke':
            ops.append('gate_revoke')
        if e.get('after_commit_before_reply'):
            require(e['observed']=={'exit_code':74,'reply_bytes':0},name+' interruption')
            ops.append(e['op']+'_lost')
    require(ops==expected['schedule'],name+' causal order')
    return dict(schedule=name,transport=ops,reads=outcomes,publication_ref=d['ref'],mandate_ref=m['ref'],
                publication_seq=d['seq'],mandate_seq=m['seq'],gate_reconciliation=gj[-1]['seq'])


def audit(root, fixtures):
    expected = read(fixtures/'expected.json')
    req, n = read(fixtures/'post.json'),read(fixtures/'notice.json')
    joined = [inspect_history(root/name,expect,req,n) for name,expect in expected.items()]
    by_name = {row['schedule']:row for row in joined}
    early,late = by_name['gate-first-notice-delayed'],by_name['post-first']
    require(early['publication_ref']==late['publication_ref'] and early['mandate_ref']==late['mandate_ref'],
            'equivalent posting-first catalogue histories')
    return joined
