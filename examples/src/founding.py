"""Offline HOLD replay. Default run checks fixture oracles and archived observation bytes."""
import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from faults import crashed_submit, overlap
from gate import canonical, initialize, submit
from reviewer import read_case_reference
from representation import load_canonical

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"
PIN = "b29384d089f192be745fa51e0b0af26abf26b5e2fbeba7209f9f21b0dd414d04"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dump_rows(path, rows):
    path.write_bytes("".join(canonical(r) + "\n" for r in rows).encode())


def snapshot(path):
    # Every observation opens a new read connection. No in-process cache participates.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        s = load_canonical(db.execute("SELECT body FROM local_state WHERE id=1").fetchone()[0])
        events = [load_canonical(x[0]) for x in db.execute("SELECT body FROM journal ORDER BY seq")]
        effects = [load_canonical(x[0]) for x in db.execute("SELECT body FROM effects ORDER BY seq")]
    return s, events, effects


def metrics(snap, aid):
    s, events, effects = snap
    a = s["actions"][aid]
    cases = sorted(a["cases"].items(), key=lambda kv:int(kv[0]))
    opened = [c for _,c in cases if c["status"] == "OPEN"]
    return dict(status=a["status"], open=len(opened), effects=sum(e["action"] == aid for e in effects),
                total_cases=len(cases), deadlines=[c["due"] for c in opened],
                overdue=sum(e["result"] == "OVERDUE" and e["action"] == aid for e in events),
                case_statuses=[c["status"] for _,c in cases])


def expand(value, aliases):
    if isinstance(value, dict):
        if set(value) == {"$seq"}:
            return aliases[value["$seq"]]["seq"]
        return {k: expand(v, aliases) for k,v in value.items()}
    if isinstance(value, list):
        return [expand(v, aliases) for v in value]
    return value


class Case:
    def __init__(self, spec, configs, state_dir):
        self.spec = spec
        self.paths, self.aliases = {}, {}
        self.rows = []
        state_dir.mkdir()
        for g in spec["gates"]:
            self.paths[g] = state_dir / (g + ".db")
            initialize(self.paths[g], configs[g])
            self.aliases[g] = {}
        self.n = 0
        self.checks = None

    def check(self, name, wanted, got):
        item = dict(check=name, expected=wanted, observed=got)
        self.checks.append(item)
        if canonical(wanted) != canonical(got):
            raise ValueError(f'{self.spec["name"]}, step {self.n}: {name}: expected {wanted!r}, observed {got!r}')

    def step(self, step):
        self.n += 1
        g = step["gate"]
        path, aliases = self.paths[g], self.aliases[g]
        tick = step["tick"]
        self.checks = []
        line = dict(trace=self.spec["name"], step=self.n, gate=g, command_tick=tick, checks=self.checks)
        before = snapshot(path)
        first_new = len(before[1])
        if "view" in step:
            r = expand(step["view"], aliases)
            line.update(operation="reviewer_view", principal=step["principal"])
            try:
                data = read_case_reference(path, FIX / "private", step["principal"], r["action"], r["case"], r["ref"])
            except PermissionError:
                self.check("reviewer access", step["allowed"], False)
            else:
                self.check("reviewer access", step["allowed"], True)
                self.check("private byte digest", step["sha256"], sha(data))
            expected_order = []
        elif "overlap" in step:
            cmds = [(tick, who, expand(cmd, aliases)) for who,cmd in step["overlap"]]
            busy, pair = overlap(path, *cmds)
            self.check("second writer at first writer's open transaction", step["busy"], busy)
            self.check("returned outcomes", step["ordered"], [r["result"] for r in pair])
            line.update(operation="two_writers", responses=pair)
            expected_order = step["ordered"]
        else:
            cmd = expand(step["command"], aliases)
            principal = step["principal"]
            args = (tick, principal, cmd)
            line.update(operation=cmd["op"], principal=principal)
            if "crash" in step:
                self.check("abrupt child exit at "+step["crash"], 86, crashed_submit(path, args, step["crash"]))
                line.update(stop=step["crash"], response_delivered=False)
                expected_order = step["durable_results"]
                if "recover_as" in step:
                    aliases[step["recover_as"]] = snapshot(path)[1][-1]
            else:
                response = submit(path, *args)
                line["response"] = response
                for key,v in expand(step["expect"],aliases).items():
                    self.check("receipt."+key, v, response.get(key))
                if "same" in step:
                    self.check("original receipt bytes", sha(canonical(aliases[step["same"]]).encode()),
                               sha(canonical(response).encode()))
                if "as" in step:
                    aliases[step["as"]] = response
                expected_order = step.get("ordered", [] if step.get("no_new") else [step["expect"]["result"]])
        after = snapshot(path)
        added = after[1][first_new:]
        self.check("new durable events in local order", expected_order, [x["result"] for x in added])
        for key in ("response","responses"):
            rs = line.get(key, [])
            for r in ([rs] if isinstance(rs,dict) else rs):
                self.check("response equals durable row "+str(r["seq"]), True, after[1][r["seq"]-1] == r)
        line["new_sequences"] = [e["seq"] for e in added]
        for statecheck in step.get("state",[]):
            other, aid = statecheck["gate"], statecheck["action"]
            actual = metrics(snapshot(self.paths[other]), aid)
            for k,wanted in statecheck.items():
                if k not in ("gate","action"):
                    self.check(f"{other}/{aid}.{k}",wanted,actual[k])
        if "crash" in step:
            line["after_reopen"] = metrics(after,cmd["action"])
            line["durable_sequence_count"] = len(after[1])
        self.rows.append(line)

    def audit(self):
        self.checks = []
        log, fx, states = [], [], []
        for g, path in self.paths.items():
            s, events, effects = snapshot(path)
            nums = [e["seq"] for e in events]
            self.check(g+" continuous local sequence",list(range(1,len(events)+1)),nums)
            ticks = [e["tick"] for e in events]
            self.check(g+" local time monotonic",sorted(ticks),ticks)
            admissions = {e["seq"]:e for e in events if e["result"]=="ADMITTED"}
            cases = {int(i):c for a in s["actions"].values() for i,c in a["cases"].items()}
            self.check(g+" each admission has one case",sorted(admissions),sorted(cases))
            for i,c in cases.items():
                rec = admissions[i]
                self.check(f"{g} case {i} original due",rec["tick"]+5,c["due"])
                self.check(f"{g} case {i} due in receipt",c["due"],rec["due"])
                overdue = [e for e in events if e["result"]=="OVERDUE" and e["case"]==i]
                self.check(f"{g} case {i} overdue count",int(c["overdue"] is not None),len(overdue))
                if overdue:
                    self.check(f"{g} case {i} overdue tick",c["due"],overdue[0]["tick"])
                self.check(f"{g} case {i} costs",s["org"],rec["cost_bearer"])
            posted = [e for e in events if e["result"]=="COMMITTED"]
            self.check(g+" effect and commitment identities",[(e["seq"],e["action"],e["target"]) for e in posted],
                       [(e["seq"],e["action"],e["target"]) for e in effects])
            self.check(g+" unique action effects",len(set(e["action"] for e in effects)),len(effects))
            # Reconstruct open sets solely from the local journal, independent of gate dispatch.
            pending = {aid:set() for aid in s["actions"]}
            for e in events:
                aid = e["action"]
                if e["result"]=="ADMITTED":
                    pending[aid].add(e["seq"])
                if e["result"] in ("RESUMED","CANCELLED"):
                    self.check(f'{g} decision {e["seq"]} full open set',sorted(pending[aid]),e["open_cases"])
                    if e["result"]=="RESUMED":
                        coverage = [events[n-1]["case"] for n in e["findings"]]
                        self.check(f'{g} decision {e["seq"]} findings coverage',sorted(pending[aid]),sorted(coverage))
                    pending[aid].clear()
                if e["result"]=="COMMITTED":
                    self.check(f'{g} commit {e["seq"]} open set',[],sorted(pending[aid]))
                if e["result"]=="LATE":
                    prior=events[e["commitment"]-1]
                    self.check(f'{g} late {e["seq"]} commitment link',[True,"COMMITTED",aid,e["submitted_claim"]],
                               [prior["seq"]<e["seq"],prior["result"],prior["action"],prior["bound_approval"]["claim"]])
            for aid,a in s["actions"].items():
                self.check(g+"/"+aid+" reconstructed open cases",sorted(pending[aid]),
                           sorted(int(k) for k,c in a["cases"].items() if c["status"]=="OPEN"))
                states.append(dict(trace=self.spec["name"],gate=g,action=aid,**metrics((s,events,effects),aid),
                                   cases=a["cases"],approval=a["approval"],approval_seq=a["approval_seq"]))
            log.extend(dict(trace=self.spec["name"],**e) for e in events)
            fx.extend(dict(trace=self.spec["name"],**e) for e in effects)
        self.rows.append(dict(trace=self.spec["name"],step="audit",checks=self.checks))
        return log,fx,states


def run(out, casefiles, custom, compare):
    configs = load(FIX/"gates.json")
    if sha((ROOT/"HOLD-v0-rules-and-replay.md").read_bytes()) != PIN:
        raise ValueError("brief digest mismatch")
    for g,s in configs.items():
        vault = load(FIX/"private"/(g+".json"))
        for a in s["actions"].values():
            for b in a["basis_catalog"]:
                d=b["claim"]["digest"]
                if sha(vault["claims"][d].encode())!=d: raise ValueError("fixture claim bytes mismatch")
            if a["routes"].keys()-vault["intake"].keys() or a["finding_routes"].keys()-vault["findings"].keys():
                raise ValueError("unresolved fixture reference")
    state_dir=out/"databases"
    state_dir.mkdir()
    evidence=out/"observations"
    evidence.mkdir()
    entries={k:[] for k in ("checks","journal","effects","states","summary")}
    for file in casefiles:
        spec=load(file)
        name=spec["name"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+",name):raise ValueError("invalid trace name")
        c=Case(spec,configs,state_dir/name)
        for step in spec["steps"]:
            c.step(step)
        log,fx,states=c.audit()
        counts=dict(trace=name,steps=len(spec["steps"]),assertions=sum(len(r["checks"]) for r in c.rows),
                    journal_rows=len(log),admissions=sum(e["result"]=="ADMITTED" for e in log),
                    decisions=sum(e["result"] in ("RESUMED","CANCELLED") for e in log),effects=len(fx),
                    outcome="PASS")
        for k,v in [("checks",c.rows),("journal",log),("effects",fx),("states",states),("summary",[counts])]:
            entries[k].extend(v)
        print(f"PASS {name} ({counts['steps']} steps, {len(fx)} effects)")
    for key,rows in entries.items():
        file=evidence/(key+".jsonl")
        dump_rows(file,rows)
        if compare and file.read_bytes() != (ROOT/"reference"/file.name).read_bytes():
            raise ValueError("saved replay differs: "+file.name)
    if not custom:
        # Cross-organisation checks use semantic identity only. Local sequence and tick are never compared.
        buyers=[e for e in entries["journal"] if e["trace"]=="02-buyers-timely" and e["result"]=="ADMITTED"]
        if (len(buyers)!=2 or len({e["challenge_digest"] for e in buyers})!=1 or
            len({e["private_ref"] for e in buyers})!=2 or len({e["gate"] for e in buyers})!=2):
            raise ValueError("shared challenge or distinct local routing mismatch")
        codes=[[e["result"] for e in entries["journal"] if e["trace"]==name and e["gate"]=="south"]
               for name in ("03-buyers-late","04-backdated")]
        if codes[0]!=codes[1]:raise ValueError("backdating changed South's outcomes")
    totals=Counter()
    for row in entries["summary"]:
        totals.update({k:row[k] for k in ("steps","assertions","journal_rows","effects")})
    print(f"PASS {len(casefiles)} traces | {totals['steps']} steps | {totals['assertions']} per-trace checks | "
          f"{totals['journal_rows']} journal rows | {totals['effects']} simulated effects")
    if compare: print("Saved reference matched byte-for-byte (5 observation files).")
