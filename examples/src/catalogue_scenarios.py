"""Two independent histories. Launch a new one-store subprocess for every operation."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from representation import canonical

BASE = Path(__file__).resolve().parent.parent


def equal(name, actual, expected):
    if actual != expected:
        raise AssertionError(f'{name}: expected {expected!r}, observed {actual!r}')


def save(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,sort_keys=True,indent=2,ensure_ascii=True)+'\n',encoding='utf-8')


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def worker(db, op, args=(), *, tick=None, crash=False):
    msg = dict(op=op,args=args)
    if tick is not None:
        msg['tick'] = tick
    if crash:
        msg['exit_before_reply'] = True
    child = subprocess.run([sys.executable,'-I','-B',str(BASE/'src/catalogue_worker.py'),str(db)],
                           input=json.dumps(msg).encode(),capture_output=True,timeout=20)
    equal(f'{op} stderr',child.stderr,b'')
    if crash:
        equal('interruption after commit, before reply',(child.returncode,child.stdout),(74,b''))
        return {'exit_code':74,'reply_bytes':len(child.stdout)}
    equal(op+' exit',child.returncode,0)
    return json.loads(child.stdout)


class History:
    def __init__(self, root):
        self.root = root
        root.mkdir()
        self.events = []
        self.checks = []

    def call(self, store, op, *args, tick=None, crash=False):
        answer = worker(self.root/(store+'.sqlite'),op,args,tick=tick,crash=crash)
        self.events.append(dict(step=len(self.events)+1,store=store,op=op,args=args,tick=tick,
                                interrupt_after_commit=crash,observed=answer))
        return answer

    def snapshot(self, phase):
        result = {}
        for store in ('gate','catalogue'):
            db = self.root/(store+'.sqlite')
            result[store] = worker(db,'snapshot')
            save(self.root/'snapshots'/phase/(store+'.json'),result[store])
            if phase == 'restart':
                shutil.copyfile(db,self.root/('restart-'+store+'.sqlite'))
        self.events.append(dict(step=len(self.events)+1,op='reopen_both',phase=phase))
        return result

    def check(self,name,actual,expected):
        self.checks.append(dict(check=name,expected=expected,observed=actual))
        equal(name,actual,expected)


def journal(tables):
    return [row['body'] for row in tables['journal']]


def action(tables):
    return tables['local_state'][0]['body']['actions']['release-DS-7']


def trace(root, name, spec, f, ex):
    h = History(root/name)
    h.call('gate','init_gate',spec)
    h.call('catalogue','init_catalogue')
    request = h.call('gate','issue',f['owner'],f['commit_command'],f['publication_id'],f['listing'],tick=8)
    original_payload = f['listing']
    changed = dict(request,payload=f['conflicting_listing'],payload_digest=sha(f['conflicting_listing']))
    cmd = f['delivery']
    delivery_key = [f['challenger'],cmd['challenge']['id'],cmd['org'],cmd['action']]
    submitted = (f['challenger'],cmd)
    block_first = name == 'block-first'
    expected_outcome = 'BLOCKED' if block_first else 'POSTED'
    if not block_first:
        h.call('catalogue','post',request,crash=True)
    pending = h.call('gate','intake',*submitted,tick=10)
    h.check('eligible durable intake', [pending['seq'],pending['tick'],pending['result'],pending['delivery_key']],
            [ex['pending_seq'],10,'PENDING',delivery_key])
    block = h.call('gate','block_message',pending['seq'])
    h.check('block identifies the dispatched request and delivery',block,dict(request=request,delivery_key=delivery_key))
    if block_first:
        h.call('catalogue','block',block['request'],block['delivery_key'],crash=True)

    reopened = h.snapshot('restart')
    g, c = reopened['gate'],reopened['catalogue']
    h.check('gate restart order',[e['result'] for e in journal(g)],ex['restart_gate_sequence'])
    h.check('catalogue restart order',[e['result'] for e in journal(c)],ex['restart_catalogue_sequence'])
    a = action(g)
    h.check('gate knowledge before reconciliation',[a['cases'],a.get('catalogue_ref'),a['status'],g['effects']],
            [{},None,'APPROVED',[]])
    receipt = c['decisions'][0]['body']
    ref = receipt['ref']
    basis = spec['actions'][cmd['action']]['approval']
    h.check('checked request and approval',
            [request['publication_id'],request['gate'],request['org'],request['action'],request['target'],
             request['approval'],request['owner'],request['payload_digest']],
            [f['publication_id'],spec['gate'],spec['org'],cmd['action'],basis['target'],basis,f['owner'],sha(original_payload)])
    h.check('durable decision joined to exact gate intent',
            [len(c['decisions']),receipt['outcome'],receipt['request'],receipt['request_digest'],
             receipt['delivery_key'],journal(g)[1]['request'],journal(g)[2]['request_digest']],
            [1,expected_outcome,request,sha(request),delivery_key if block_first else None,request,sha(request)])
    expected_rows = [] if block_first else [dict(publication_id=request['publication_id'],ref=ref,body=original_payload)]
    h.check('listing on restart',c['listings'],expected_rows)
    if block_first:
        h.check('pending challenge retry after restart',h.call('gate','intake',*submitted,tick=11),pending)
        for phase,label in (('delayed-post','in-flight post'),('delayed-retry','exact retry of delayed post')):
            h.check(label,h.call('catalogue','post',request),dict(result='BLOCKED',receipt=receipt))
            after = h.snapshot(phase)
            h.check(label+' durable rows, block and gate knowledge',
                    [after['catalogue']['listings'],after['catalogue']['decisions'],
                     journal(after['gate']),action(after['gate']),after['gate']['effects']],
                    [[],c['decisions'],journal(g),a,[]])
    else:
        # A genuine block reply for a different payload, even though the ID, approval
        # and challenge key match. Isolated control store. It has no visible listing.
        h.call('control','init_catalogue')
        wrong = h.call('control','block',changed,delivery_key)['receipt']
        save(h.root/'control.json',worker(h.root/'control.sqlite','snapshot'))
        h.check('wrong request control binding',
                [wrong['request']['publication_id'],wrong['request']['approval']['id'],wrong['delivery_key'],
                 wrong['request_digest']==sha(request),wrong['ref']==ref],
                [request['publication_id'],basis['id'],delivery_key,False,False])
        for tick,label,reply in ((11,'withheld',None),(12,'wrong-request',wrong)):
            answer = h.call('gate','reconcile',pending['seq'],reply,tick=tick)
            repeat = h.call('gate','intake',*submitted,tick=tick)
            h.check(label+' recovery and repeat',[answer,repeat],[pending,pending])
            s = h.snapshot(label)
            gj = journal(s['gate'])
            h.check(label+' no gate final, new post, new ID or hold',
                    [[r['result'] for r in gj],action(s['gate'])['cases'],
                     [r['request']['publication_id'] for r in gj if r['result']=='POST_INTENT'],
                     action(s['gate']).get('catalogue_ref'),s['gate']['effects']],
                    [ex['gate_sequence'][:tick-7],{},[f['publication_id']],None,[]])
            h.check(label+' recorded lookup reason and evidence',
                    [gj[-1]['reason'],gj[-1]['presented']],
                    ['LOOKUP_UNAVAILABLE' if reply is None else 'EVIDENCE_MISMATCH',reply])
            h.check(label+' preserves actual catalogue',s['catalogue'],c)
    conflict = h.call('catalogue','post',changed)
    h.check('same publication and revision, changed payload conflict',conflict,
            dict(result='CONFLICT',reason='REQUEST_CONFLICT',original_ref=ref))
    lookup = h.call('catalogue','lookup',request['publication_id'])
    h.check('lookup recovers first durable decision after conflict',lookup,receipt)
    final = h.call('gate','reconcile',pending['seq'],lookup,tick=13)
    h.check('final decision at actual gate sequence and tick',
            [final['seq'],final['tick'],final['result'],final['intake_tick'],final['pending_seq'],
             final['catalogue_ref'],final['approval_revision'],final['publication_id'],
             final['checked_basis'],final['reviewer'],final.get('due')],
            [ex['final_seq'],13,ex['gate_sequence'][-1],10,pending['seq'],ref,basis['id'],
             f['publication_id'],basis,ex['reviewer'],ex['due']])
    h.check('final evidence and challenge', [final['catalogue_receipt'],final['delivery_key'],
            final['challenge'],final['ground'],final['private_ref']],
            [receipt,delivery_key,'C-A','DATASET_SCOPE_MISMATCH','r-48d1'])
    if block_first:
        h.check('deadline starts at durable admission',final['due']-final['tick'],5)
    else:
        h.check('late links original actual posting and claim',
                [final['commitment'],cmd['challenge']['claim']],[ref,receipt['request']['approval']['claim']])
        h.check('exact posting retry after reconciliation',h.call('catalogue','post',request),
                dict(result='POSTED',receipt=receipt))
    h.check('challenge retry after another reopen',h.call('gate','intake',*submitted,tick=14),final)
    s = h.snapshot('final')
    g, c = s['gate'],s['catalogue']
    gj,cj = journal(g),journal(c)
    holds = [v for v in action(g)['cases'].values() if v['status']=='OPEN']
    h.check('ordered gate events',[r['result'] for r in gj],ex['gate_sequence'])
    h.check('local ticks',[r['tick'] for r in gj],ex['gate_ticks'])
    h.check('ordered catalogue attempts',[r['operation'] for r in cj],ex['catalogue_operations'])
    h.check('ordered catalogue results',[r['result'] for r in cj],ex['catalogue_sequence'])
    h.check('all attempts refer to original durable decision',[r['original_ref'] for r in cj],[ref]*len(cj))
    h.check('single stable intent and outcome', [sum(r['result']=='POST_INTENT' for r in gj),
            sum(r['new_decision'] for r in cj),len(c['decisions'])],
            [ex['dispatches'],ex['original_decisions'],1])
    h.check('final gate effect, catalogue listing and original decision',
            [g['effects'],c['listings'],c['decisions']], [[],expected_rows,reopened['catalogue']['decisions']])
    h.check('final visible rows and open holds',[len(c['listings']),len(holds),action(g)['status']],
            [ex['visible'],ex['open'],ex['gate_status']])
    if block_first:
        h.check('persisted hold identity',[holds[0]['assignment'],holds[0]['reviewer'],holds[0]['due']],
                [final['seq'],ex['reviewer'],ex['due']])
    h.check('durable retry index',g['local_state'][0]['body']['deliveries'],
            {canonical(delivery_key):{'seq':final['seq'],'digest':pending['delivery_digest']}})
    summary = dict(history=name,publication_id=request['publication_id'],approval_revision=basis['id'],
                   request_digest=sha(request),catalogue_ref=ref,catalogue_seq=receipt['seq'],
                   pending_seq=pending['seq'],intake_tick=pending['tick'],final_seq=final['seq'],
                   disposition=final['result'],final_tick=final['tick'],due=final.get('due'),
                   reviewer=final['reviewer'],open_holds=len(holds),visible_DS7=len(c['listings']),
                   checks=len(h.checks),post_intents=sum(r['result']=='POST_INTENT' for r in gj))
    save(h.root/'transcript.json',h.events)
    save(h.root/'checks.json',h.checks)
    return summary
