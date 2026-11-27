"""Offline transport, scheduled interruptions, and expected-versus-observed checks."""
import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from gate import digest
from representation import canonical

ROOT = Path(__file__).resolve().parent.parent
REQUEST_DIGEST = 'a6d0fae7aa450d52b5fb6a5bd4623a144d9bb555b2c26b922cc5beeccede07a4'
NAMES = ('revoke-first','post-first','gate-first-notice-delayed')


def read(path):
    return json.loads(path.read_bytes())


def save(path, obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,indent=2,sort_keys=True)+'\n',encoding='utf-8')


def eq(label, actual, expected):
    if canonical(actual) != canonical(expected):
        raise AssertionError(f'{label}: observed {actual!r}, expected {expected!r}')


def call(db, op, args=(), tick=None, loss=False):
    envelope = dict(op=op,args=args,tick=tick,lose_reply=loss)
    cp = subprocess.run([sys.executable,'-I','-B',str(ROOT/'src/revocation_worker.py'),str(db)],
                        input=json.dumps(envelope).encode(),capture_output=True,timeout=20)
    if cp.stderr or cp.returncode != (74 if loss else 0):
        raise RuntimeError(f'{op}: worker exit {cp.returncode}, stderr {cp.stderr!r}')
    if loss:
        eq('reply length at loss boundary',len(cp.stdout),0)
        return dict(exit_code=cp.returncode,reply_bytes=0)
    return json.loads(cp.stdout)


def tables(snap, store):
    return snap[store]['tables']


def state(snap):
    return tables(snap,'gate')['local_state'][0]['body']


def events(snap, store):
    return [r['body'] for r in tables(snap,store)['journal']]


class History:
    def __init__(self, path):
        self.path = path
        path.mkdir()
        self.log = []
        self.checks = []

    def note(self, **event):
        self.log.append(dict(step=len(self.log)+1,**event))

    def path_for(self, store):
        return self.path/(store+'.sqlite')

    def invoke(self, store, op, *args, tick=None, loss=False):
        file = self.path_for(store)
        file.parent.mkdir(parents=True,exist_ok=True)
        observed = call(file,op,args,tick,loss)
        self.note(store=store,op=op,args=args,tick=tick,after_commit_before_reply=loss,observed=observed)
        return observed

    def snapshot(self, label, copy_files=False):
        data = {}
        for store in ('gate','catalogue'):
            path = self.path_for(store)
            data[store] = call(path,'inspect')
            if copy_files:
                shutil.copyfile(path,self.path/('restart-'+store+'.sqlite'))
        save(self.path/'snapshots'/(label+'.json'),data)
        self.note(op='reopen_read_only',snapshot=label)
        return data

    def check(self, name, actual, expected):
        self.checks.append(dict(check=name,expected=expected,observed=actual))
        eq(name,actual,expected)


def authority_config(spec, aid):
    action = spec['actions'][aid]
    return dict(scope=dict(gate=spec['gate'],org=spec['org'],action=aid,target=action['target']),
                subject=action['owner'],registrar=spec['registrar'])


def wrong_evidence(h, spec, f, n, req, rev_wins):
    # Each negative source commits a genuine receipt in a separate scratch catalogue.
    # The first has the original approval and publication ID but a different posting key.
    actor = n['registrar']
    other = dict(req,command_key='control-post-key')
    store = 'controls/other-request'
    h.invoke(store,'init_catalogue',authority_config(spec,req['action']))
    if rev_wins:
        h.invoke(store,'notice',actor,n)
    h.invoke(store,'post',other)
    if not rev_wins:
        h.invoke(store,'notice',actor,n)
    wrong_request, _ = h.invoke(store,'lookup',req['publication_id'],n)
    # The second genuine notice comes from the same trusted registrar for another target.
    wrong_spec = copy.deepcopy(spec)
    aid = req['action']
    alt = wrong_spec['actions'][aid]
    alt['target'] = alt['approval']['target'] = 'DS-7 private control catalogue'
    g, c = 'controls/other-scope-gate','controls/other-scope-catalogue'
    h.invoke(g,'init_gate',wrong_spec)
    h.invoke(g,'gate_issue',f['owner'],dict(f['commit_command'],target=alt['target']),
             req['publication_id'],f['listing'],tick=8)
    other_notice = h.invoke(g,'gate_revoke',actor,aid,tick=9)
    h.invoke(c,'init_catalogue',authority_config(wrong_spec,aid))
    h.invoke(c,'notice',actor,other_notice)
    _,wrong_scope = h.invoke(c,'lookup',req['publication_id'],other_notice)
    h.check('wrong-request receipt differs only by posting key',wrong_request['request'],other)
    h.check('genuine wrong-scope notice differs only by target',other_notice,
            dict(n,scope=dict(n['scope'],target=alt['target'])))
    return wrong_request,wrong_scope


def history(out, name, ex, spec, f):
    h = History(out/name)
    req = read(ROOT/'fixtures/post.json')
    n = read(ROOT/'fixtures/notice.json')
    aid, pub, actor = req['action'],req['publication_id'],n['registrar']
    rev_wins = name=='revoke-first'
    h.invoke('gate','init_gate',spec)
    h.invoke('catalogue','init_catalogue',authority_config(spec,aid))
    wire = h.invoke('gate','gate_issue',f['owner'],f['commit_command'],pub,f['listing'],tick=8)
    h.check('frozen request actually issued',wire,req)
    # The queue owns this immutable message from dispatch until its scheduled delivery.
    h.note(op='dispatch',source='gate',destination='catalogue',frame=wire)
    schedule = ['dispatch']

    def revoke():
        obtained = h.invoke('gate','gate_revoke',actor,aid,tick=9)
        h.check('atomic gate change emitted frozen notice',obtained,n)
        schedule.append('gate_revoke')
        return obtained

    if name=='post-first':
        h.invoke('catalogue','post',wire,loss=True)
        schedule.append('post_lost')
        revoke()
    else:
        notice = revoke()
        if rev_wins:
            h.invoke('catalogue','notice',actor,notice,loss=True)
            schedule.append('notice_lost')
        else:
            # Both databases inspected between the gate commit and the delayed post.
            before = h.snapshot('gate-revoked-before-post')
            h.check('in-flight gap: gate inactive, catalogue active, no listing',
                    [state(before)['actors'][f['owner']]['active'],tables(before,'catalogue')['authority'][0]['body']['active'],
                     len(tables(before,'catalogue')['listings'])],[False,True,0])
            h.invoke('catalogue','post',wire,loss=True)
            schedule.append('post_lost')
    h.check('observed transport order before restart',schedule,ex['schedule'])
    cut = h.snapshot('restart',copy_files=True)
    g = state(cut)
    a = g['actions'][aid]
    c = tables(cut,'catalogue')
    auth = c['authority'][0]['body']
    h.check('catalogue restart revision, active flag, listings and decisions',
            [auth['revision'],auth['active'],len(c['listings']),len(c['decisions'])],ex['cut'])
    h.check('gate restart intent pending, owner inactive, notice recoverable, no holds',
            [a['intent_seq'],a.get('resolution_seq'),g['actors'][f['owner']],a['revocation'],a['cases']],
            [2,None,dict(role='release manager',revision=2,active=False),n,{}])
    h.check('gate restart journal order',[e['result'] for e in events(cut,'gate')],
            ['APPROVED','POST_INTENT','LOCAL_REVOCATION'])
    h.check('restart catalogue first durable record only',
            [len(events(cut,'catalogue')),len(c['changes'])],[1,int(rev_wins)])
    first = c['changes' if rev_wins else 'decisions'][0]['body']
    h.check('lost response identifies actual durable first receipt',events(cut,'catalogue')[0]['reply']['receipt'],first)
    h.check('lost operation process exit and empty stdout',
            [e['observed'] for e in h.log if e.get('after_commit_before_reply')],[dict(exit_code=74,reply_bytes=0)])
    resend = h.invoke('gate','read_notice',aid)
    h.check('recover notice after gate restart',resend,n)
    fresh_cmd = dict(f['commit_command'],key='fresh-gate-intent')
    denied = h.invoke('gate','gate_issue',f['owner'],fresh_cmd,pub,f['listing'],tick=10)
    h.check('fresh revision-1 intent refused on authority',[denied['result'],denied['reason']],['INTENT_REFUSED','STALE_AUTHORITY'])
    if rev_wins:
        recovered = h.invoke('catalogue','post',wire)
    else:
        recovered = h.invoke('catalogue','notice',actor,resend)
    decision, change = h.invoke('catalogue','lookup',pub,resend)
    h.check('first durable receipt survived recovery',change if rev_wins else decision,first)
    h.check('second durable receipt matches delivery',recovered['receipt'],decision if rev_wins else change)
    h.check('two catalogue sequences and publication result',
            [change['seq'],decision['seq'],decision['result']],
            [ex['mandate_seq'],ex['decision_seq'],ex['outcome']])
    h.check('distinct first references',decision['ref'] != change['ref'],True)
    h.check('publication identity and notice provenance',
            [decision['request'],decision['request_digest'],change['notice'],change['notice_digest']],
            [req,REQUEST_DIGEST,n,digest(n)])
    checked = decision['checked_mandate']
    h.check('mandate checked by first publication transaction',checked,
            dict(scope=n['scope'],subject=n['subject'],revision=2 if rev_wins else 1,
                 active=not rev_wins,change_ref=change['ref'] if rev_wins else None))
    h.check('decision reason',decision['reason'],'SUPERSEDED_MANDATE' if rev_wins else 'ACTIVE_MANDATE')
    rows = [] if rev_wins else [dict(id=pub,ref=decision['ref'],body=req['payload'])]
    before_controls = h.snapshot('both-decisions')
    h.check('actual listings after both decisions',tables(before_controls,'catalogue')['listings'],rows)
    original = tables(before_controls,'catalogue')
    wrong_req,wrong_scope = wrong_evidence(h,spec,f,n,req,rev_wins)
    h.check('wrong-request control has same result and catalogue sequence',
            [wrong_req['result'],wrong_req['seq']],[decision['result'],decision['seq']])
    pending = dict(result='PENDING',intent_seq=2,publication_id=pub)
    for tick,(label,d,m) in enumerate([
            ('withheld',None,None),('wrong-request',wrong_req,change),
            ('wrong-scope',decision,wrong_scope),('correct-revocation-only',None,change)],11):
        result = h.invoke('gate','reconcile',aid,d,m,tick=tick)
        h.check(label+' returns original pending intent',result,pending)
        snapshot = h.snapshot(label)
        journal = events(snapshot,'gate')
        h.check(label+' leaves one intent, no final or hold, no new dispatch',
                [[e['publication_id'] if 'publication_id' in e else e['request']['publication_id']
                  for e in journal if e['result']=='POST_INTENT'],
                 sum(e['result']=='RECONCILED' for e in journal),state(snapshot)['actions'][aid]['cases'],
                 sum(e['op']=='dispatch' for e in h.log)],[[pub],0,{},1])
        h.check(label+' retains exact gate observation',
                [journal[-1]['result'],journal[-1]['presented_decision'],journal[-1]['presented_change']],
                ['LOOKUP_INCONCLUSIVE',d,m])
        h.check(label+' preserves primary catalogue rows',tables(snapshot,'catalogue'),original)
    # Recover only the posting/refusal frame. The gate retained the verified notice
    # receipt separately, including through the last inconclusive lookup and restart.
    decision_again, _ = h.invoke('catalogue','lookup',pub,n)
    final = h.invoke('gate','reconcile',aid,decision_again,None,tick=15)
    h.check('final gate reconciliation uses actual local sequence and joins both originals',
            [final['seq'],final['tick'],final['result'],final['reason'],final['intent_seq'],final['gate_event'],
             final['publication_id'],final['approval_revision'],final['request_digest'],
             final['decision'],final['change'],final['publication_ref'],final['revocation_ref']],
            [9,15,'RECONCILED',ex['outcome'],2,n['gate_event'],pub,req['approval']['id'],REQUEST_DIGEST,
             decision,change,decision['ref'],change['ref']])
    post_retry = h.invoke('catalogue','post',wire)
    notice_retry = h.invoke('catalogue','notice',actor,resend)
    h.check('exact post retry after revocation',post_retry,dict(result=ex['outcome'],receipt=decision))
    h.check('exact notice retry after revocation',notice_retry,dict(result='REVOKED',receipt=change))
    altered = dict(req,command_key='control-post-key')
    h.check('change only posting key records linked conflict',h.invoke('catalogue','post',altered),
            dict(result='CONFLICT',reason='POST_ID_CONFLICT',original_ref=decision['ref']))
    h.check('altered notice under original identity records linked conflict',
            h.invoke('catalogue','notice',actor,dict(n,active=True)),
            dict(result='CONFLICT',reason='NOTICE_ID_CONFLICT',original_ref=change['ref']))
    stale = dict(n,prior_revision=2,revision=1,active=True,gate_event=dict(gate=n['issuer'],seq=99))
    h.check('separate stale update refused',h.invoke('catalogue','notice',actor,stale),
            dict(result='REFUSED',reason='STALE_REVISION'))
    h.check('matching readback after conflicts',h.invoke('catalogue','lookup',pub,n),[decision,change])
    h.check('exact gate resolution retry returns original',h.invoke('gate','reconcile',aid,decision,change,tick=16),final)
    end = h.snapshot('final')
    c = tables(end,'catalogue')
    h.check('decisions, changes, authority and listing rows after all controls',
            [c[k] for k in ('decisions','changes','authority','listings')],
            [original[k] for k in ('decisions','changes','authority','listings')])
    cj = events(end,'catalogue')
    h.check('durable main catalogue results in order',[e['reply']['result'] for e in cj],
            (['REVOKED','REFUSED'] if rev_wins else ['POSTED','REVOKED'])+
            [ex['outcome'],'REVOKED','CONFLICT','CONFLICT','REFUSED'])
    h.check('catalogue replies recovered from journal after all retries',
            [e['reply']['receipt'] for e in cj[2:4]],[decision,change])
    gj = events(end,'gate')
    h.check('contiguous gate order and final row',[e['seq'] for e in gj],list(range(1,10)))
    h.check('final holdings and effects in both main stores',
            [len(c['listings']),len(c['changes']),len(c['decisions']),len(tables(end,'gate')['effects']),
             len(state(end)['actions'][aid]['cases']),sum(e['result']=='POST_INTENT' for e in gj),
             sum(e['result']=='RECONCILED' for e in gj),g['actions'][aid]['approval'],
             state(end)['actions'][aid]['approval'],state(end)['actors'][f['owner']]['revision'],
             state(end)['actors'][f['owner']]['active'],c['authority'][0]['body']['revision'],
             c['authority'][0]['body']['active']],
            [ex['visible'],1,1,0,0,1,1,req['approval'],req['approval'],2,False,2,False])
    # A new prospective publication distinguishes a genuinely fresh authority check
    # from a conflict or an exact replay. Probe an isolated copy of this final DB.
    probe = 'controls/fresh-use'
    shutil.copyfile(h.path_for('catalogue'),h.path_for(probe))
    h.note(op='clone_for_fresh_use',source='catalogue',destination=probe)
    fresh = dict(req,publication_id=pub+'/fresh-control')
    rejection = h.invoke(probe,'post',fresh)
    h.check('first decision for a fresh ID under inactive authority',
            [rejection['result'],rejection['receipt']['reason'],rejection['receipt']['checked_mandate']['change_ref']],
            ['REFUSED','SUPERSEDED_MANDATE',change['ref']])
    probe_snap = call(h.path_for(probe),'inspect')['tables']
    h.check('fresh-use probe adds only a refusal and leaves listing count unchanged',
            [len(probe_snap['decisions']),probe_snap['listings'],len(probe_snap['changes'])],[2,rows,1])
    save(h.path/'transcript.json',h.log)
    save(h.path/'checks.json',h.checks)
    return dict(schedule=name,request_digest=REQUEST_DIGEST,visible=len(c['listings']),
                gate_reconciliation=final['seq'],mandate_seq=change['seq'],decision_seq=decision['seq'],
                mandate_ref=change['ref'],publication_ref=decision['ref'],outcome=decision['result'],
                open_holds=0,original_dispatches=1,named_checks=len(h.checks))
