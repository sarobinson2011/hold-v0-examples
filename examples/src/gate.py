"""HOLD v0. One SQLite writer order per local gate. No workflow-specific dispatch."""
import copy
import hashlib
import sqlite3
from contextlib import closing

from messages import valid
from representation import canonical, effect_action_key, load_canonical


def digest(obj):
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def connect(path, timeout=15):
    conn = sqlite3.connect(path, isolation_level=None, timeout=timeout)
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def record(conn, state, tick, who, action, result, reason, **details):
    actors = state["actors"]
    a = state["actions"].get(action)
    seq = conn.execute("SELECT coalesce(max(seq),0)+1 FROM journal").fetchone()[0]
    event = dict(gate=state["gate"], org=state["org"], seq=seq, tick=tick,
                 actor=who if who in actors else None, authenticated=who in actors, role=actors.get(who, {}).get("role"),
                 action=action, claim=a["approval"]["claim"] if a else None,
                 result=result, reason=reason, **details)
    conn.execute("INSERT INTO journal VALUES (?,?)", (seq, canonical(event)))
    return event


def initialize(path, spec):
    with closing(connect(path)) as db:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE local_state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
        db.execute("CREATE TABLE journal (seq INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("CREATE TABLE effects (action TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, body TEXT NOT NULL)")
        state = copy.deepcopy(spec)
        state.update(tick=0, deliveries={}, challenges={}, commits={})
        for aid, a in state["actions"].items():
            a.update(status="APPROVED", cases={}, findings={}, used_approvals=[a["approval"]["id"]])
            initial = record(db, state, 0, a["owner"], aid, "APPROVED", "ENROLLED",
                             target=a["target"], approval=a["approval"], reviewer=a["reviewer"],
                             escalation_reviewer=a["lead"], period=a["period"],
                             grounds=a["grounds"], roles=a["roles"], intake=a["intake"],
                             cost_bearer=state["org"], cost_items=a["cost_items"])
            a["approval_seq"] = initial["seq"]
        db.execute("INSERT INTO local_state VALUES (1,?)", (canonical(state),))
        db.commit()


def event_at(conn, seq):
    return load_canonical(conn.execute("SELECT body FROM journal WHERE seq=?", (seq,)).fetchone()[0])


def active(state, actor):
    return state["actors"].get(actor, {}).get("active", False)


def open_cases(a):
    return {k: c for k, c in a["cases"].items() if c["status"] == "OPEN"}


def advance(conn, s, tick):
    if type(tick) is not int or tick < s["tick"]:
        raise ValueError("local tick must be a nondecreasing integer")
    pending = sorted((c["due"], int(k), aid, c)
                     for aid, a in s["actions"].items() for k, c in open_cases(a).items()
                     if c["overdue"] is None and c["due"] <= tick)
    for due, seq, aid, c in pending:
        lead = s["actions"][aid]["lead"]
        e = record(conn, s, due, s["clock"], aid, "OVERDUE", "DUE_REACHED", case=seq,
                   challenge=c["challenge"], reviewer=c["reviewer"], escalation_reviewer=lead, due=due,
                   petitions=c["petitions"])
        c["overdue"] = e["seq"]
    s["tick"] = tick


def approval_error(s, a, proposal, reviewers):
    if proposal.get("target") != a["target"]:
        return "TARGET_MISMATCH"
    if (proposal.get("claim"), proposal.get("evidence")) != (a["current"]["claim"], a["current"]["evidence"]):
        return "STALE_EVIDENCE"
    if not active(s, a["owner"]) or proposal.get("owner_mandate") != s["actors"][a["owner"]]["revision"]:
        return "STALE_AUTHORITY"
    mandates = {name: s["actors"][name]["revision"] for name in reviewers}
    if proposal.get("reviewer_mandates") != mandates or any(not active(s, name) for name in reviewers):
        return "STALE_AUTHORITY"
    return None


class Transaction:
    def __init__(self, conn, state, tick, principal, command):
        self.db, self.s, self.tick, self.who, self.cmd = conn, state, tick, principal, command
        claimed_action = command.get("action") if isinstance(command, dict) else None
        self.aid = claimed_action if isinstance(claimed_action, str) else None
        self.a = state["actions"].get(self.aid)

    def log(self, result, reason, **fields):
        return record(self.db, self.s, self.tick, self.who, self.aid, result, reason, **fields)

    def dispatch(self):
        op = self.cmd.get("op") if isinstance(self.cmd, dict) else None
        if self.who not in self.s["actors"]:
            return self.log("REJECTED", "UNAUTHENTICATED", operation=op)
        if not valid(self.cmd):
            # Only authenticated, addressable deliveries have retry keys.
            if op == "deliver" and self.aid and isinstance(self.cmd.get("org"), str):
                core = self.cmd.get("challenge")
                if isinstance(core, dict) and isinstance(core.get("id"), str) and core["id"].strip():
                    key = canonical([self.who, core["id"], self.cmd["org"], self.aid])
                    # Only well-formed envelopes may disregard the transport timestamp.
                    fp = digest(["malformed", self.cmd])
                    old = self.s["deliveries"].get(key)
                    if old:
                        if old["digest"] == fp:
                            return event_at(self.db, old["seq"])
                        return self.log("REJECTED", "DELIVERY_KEY_CONFLICT", prior=old["seq"], challenge=core["id"])
                    receipt = self.log("REJECTED", "MALFORMED_COMMAND", challenge=core["id"])
                    self.s["deliveries"][key] = dict(digest=fp, seq=receipt["seq"])
                    return receipt
            return self.log("REJECTED", "MALFORMED_COMMAND")
        if op == "deliver":
            return self.deliver()
        if op == "advance":
            if self.who != self.s["clock"]:
                return self.log("REJECTED", "CLOCK_AUTHORITY_REQUIRED")
            return self.log("ADVANCED", "LOCAL_CLOCK")
        if op == "mandate":
            return self.mandate()
        if op == "commit":
            return self.commit()
        if not self.a:
            return self.log("REJECTED", "UNKNOWN_ACTION")
        methods = {"prepare": self.prepare, "update_basis": self.update_basis,
                   "petition": self.petition, "escalate": self.escalate, "finding": self.finding,
                   "resume": self.resolve, "cancel": self.resolve}
        return methods[op]()

    def deliver(self):
        s, a, cmd, who = self.s, self.a, self.cmd, self.who
        core = cmd["challenge"]
        cid = core["id"]
        core_key = canonical([who, cid])
        key = canonical([who, cid, cmd["org"], self.aid])
        # Sender time belongs to the transport envelope. It never enters the gate clock.
        fingerprint = digest({k: v for k, v in cmd.items() if k != "sent_tick"})
        meta = dict(challenge=cid, submitted_by=core["challenger"], addressed_org=cmd["org"],
                    submitted_claim=core["claim"], ground=core["ground"], delivery_digest=fingerprint,
                    challenge_digest=digest(core), sent_tick=cmd.get("sent_tick"))
        old = s["deliveries"].get(key)
        if old:
            if old["digest"] == fingerprint:
                return event_at(self.db, old["seq"])
            return self.log("REJECTED", "DELIVERY_KEY_CONFLICT", prior=old["seq"], **meta)
        result = "REJECTED"
        reason = None
        if who != core["challenger"]:
            reason = "CHALLENGER_IDENTITY_MISMATCH"
        elif cmd["org"] != s["org"] or not a:
            reason = "SCOPE_MISMATCH"
        elif not active(s, who) or s["actors"][who]["role"] not in a["roles"]:
            reason = "INELIGIBLE_CHALLENGER"
        elif a["status"] == "CANCELLED":
            result, reason = "CLOSED", "ACTION_CANCELLED"
            meta["decision"] = a["decision"]
        elif core_key in s["challenges"] and s["challenges"][core_key] != digest(core):
            reason = "CHALLENGE_ID_CONFLICT"
        elif core["claim"] != a["approval"]["claim"]:
            reason = "RELIANCE_MISMATCH"
        elif core["ground"] not in a["grounds"]:
            reason = "GROUND_NOT_ALLOWED"
        elif (a["routes"].get(cmd["ref"]) != a["reviewer"] or not active(s, a["reviewer"])):
            reason = "REVIEWER_ROUTE_UNAVAILABLE"
        else:
            s["challenges"][core_key] = digest(core)
            meta.update(private_ref=cmd["ref"], reviewer=a["reviewer"], basis_approval=a["approval_seq"])
            if a["status"] == "COMMITTED":
                result, reason = "LATE", "ALREADY_COMMITTED"
                meta["commitment"] = a["commit_seq"]
            else:
                result, reason = "ADMITTED", "TIMELY"
                meta.update(due=self.tick + a["period"], escalation_reviewer=a["lead"],
                            cost_bearer=s["org"], cost_items=a["cost_items"])
        receipt = self.log(result, reason, **meta)
        s["deliveries"][key] = {"digest": fingerprint, "seq": receipt["seq"]}
        if result == "ADMITTED":
            seq = receipt["seq"]
            a["cases"][str(seq)] = dict(challenge=cid, challenger=who, reviewer=a["reviewer"],
                assignment=seq, due=meta["due"], overdue=None, petitions=[], status="OPEN", ref=cmd["ref"])
            a["status"] = "HELD"
        return receipt

    def prepare(self):
        if self.who != self.a["owner"] or not active(self.s, self.who):
            return self.log("REJECTED", "OWNER_REQUIRED")
        return self.log("PREPARED", "REVERSIBLE_ONLY", status=self.a["status"])

    def update_basis(self):
        s, a, cmd = self.s, self.a, self.cmd
        if self.who != s["registrar"] or not active(s, self.who):
            return self.log("REJECTED", "REGISTRAR_REQUIRED")
        # Fixture register pins ordered, immutable claim/evidence revisions for each action.
        wanted = cmd["basis"]
        catalog = a["basis_catalog"]
        if wanted not in catalog or catalog.index(wanted) <= catalog.index(a["current"]):
            return self.log("REFUSED", "UNKNOWN_OR_SUPERSEDED_BASIS")
        a["current"] = wanted
        return self.log("UPDATED", "CURRENT_BASIS", current=wanted)

    def mandate(self):
        s, cmd = self.s, self.cmd
        if self.who != s["registrar"] or not active(s, self.who):
            return self.log("REJECTED", "REGISTRAR_REQUIRED")
        entry = s["actors"].get(cmd["subject"])
        if not entry or type(cmd["revision"]) is not int or cmd["revision"] <= entry["revision"] or type(cmd["active"]) is not bool:
            return self.log("REFUSED", "INVALID_MANDATE_UPDATE")
        entry.update(revision=cmd["revision"], active=cmd["active"])
        return self.log("UPDATED", "CURRENT_MANDATE", subject=cmd["subject"], mandate=entry)

    def case(self):
        return open_cases(self.a).get(str(self.cmd["case"]))

    def case_fields(self, c):
        return dict(case=self.cmd["case"], challenge=c["challenge"], reviewer=c["reviewer"],
                    due=c["due"], escalation_reviewer=self.a["lead"])

    def petition(self):
        c = self.case()
        if not c:
            return self.log("REFUSED", "CASE_NOT_OPEN", case=self.cmd["case"])
        if self.who not in (c["challenger"], self.a["owner"]) or not active(self.s, self.who):
            return self.log("REFUSED", "PETITIONER_INELIGIBLE", **self.case_fields(c))
        event = self.log("PETITION_RECEIVED", "DELAY_REVIEW_REQUESTED", **self.case_fields(c))
        c["petitions"].append(event["seq"])
        return event

    def escalate(self):
        a, cmd = self.a, self.cmd
        c = self.case()
        if not c:
            return self.log("REFUSED", "CASE_NOT_OPEN", case=cmd["case"])
        fields = self.case_fields(c)
        if self.who != a["lead"] or not active(self.s, self.who):
            return self.log("REFUSED", "ESCALATION_REVIEWER_REQUIRED", **fields)
        if not cmd.get("reason", "").strip():
            return self.log("REFUSED", "REASON_REQUIRED", **fields)
        fields.update(overdue=c["overdue"], petitions=c["petitions"], note=cmd["reason"])
        if "replacement" not in cmd:
            return self.log("DECISION_REQUESTED", "ESCALATION_REVIEW", **fields)
        new = cmd["replacement"]
        if new not in a["reviewer_pool"] or not active(self.s, new):
            return self.log("REFUSED", "REPLACEMENT_UNAUTHORISED", **fields)
        event = self.log("REASSIGNED", "REVIEW_TRANSFER", replacement=new, private_ref=c["ref"], **fields)
        # The assignment grants the replacement access to this case reference only.
        c.update(reviewer=new, assignment=event["seq"])
        return event

    def finding(self):
        a, cmd, who = self.a, self.cmd, self.who
        c = self.case()
        if not c:
            return self.log("REFUSED", "CASE_NOT_OPEN", case=cmd["case"])
        fields = self.case_fields(c)
        if who != c["reviewer"] or not active(self.s, who) or self.s["actors"][who]["revision"] != cmd["mandate"]:
            return self.log("REFUSED", "REVIEWER_UNAUTHORISED", **fields)
        if cmd["basis"] != a["current"]:
            return self.log("REFUSED", "STALE_EVIDENCE", **fields)
        route = dict(reviewer=who, challenge=c["challenge"], challenger=c["challenger"], basis=cmd["basis"])
        if a["finding_routes"].get(cmd["ref"]) != route:
            return self.log("REFUSED", "REVIEWER_ROUTE_UNAVAILABLE", **fields)
        event = self.log("FINDING", "REVIEW_RECORDED", basis=cmd["basis"], mandate=cmd["mandate"],
                         private_ref=cmd["ref"], assignment=c["assignment"], **fields)
        c["latest_finding"] = event["seq"]
        a["findings"][str(event["seq"])] = event
        return event

    def resolve(self):
        a, cmd, who = self.a, self.cmd, self.who
        cases = open_cases(a)
        resume = cmd["op"] == "resume"
        ids = cmd["findings"]
        records = [a["findings"].get(str(i)) for i in ids]
        fields = dict(owner=who, open_cases=sorted(map(int, cases)), findings=ids, note=cmd["reason"],
                      current=a["current"], prior_approval=a["approval_seq"], owner_mandate=cmd["owner_mandate"],
                      reviewers={k:c["reviewer"] for k,c in cases.items()},
                      checked_owner=self.s["actors"][a["owner"]],
                      checked_reviewers={c["reviewer"]:self.s["actors"][c["reviewer"]] for c in cases.values()})
        if resume:
            fields["proposed_approval"] = cmd["approval"]
        def refuse(code):
            return self.log("REFUSED", code, **fields)
        if a["status"] != "HELD" or not cases:
            return refuse("ACTION_NOT_HELD")
        if who != a["owner"] or not active(self.s, who) or self.s["actors"][who]["revision"] != cmd["owner_mandate"]:
            return refuse("STALE_AUTHORITY")
        if not cmd["reason"].strip():
            return refuse("REASON_REQUIRED")
        if resume:
            p = cmd["approval"]
            err = approval_error(self.s, a, p, {c["reviewer"] for c in cases.values()})
            if err:
                return refuse(err)
            if not p.get("id") or p["id"] in a["used_approvals"]:
                return refuse("FRESH_APPROVAL_REQUIRED")
        if not records or any(f is None for f in records) or len(set(ids)) != len(ids):
            return refuse("FINDINGS_INCOMPLETE")
        seen = set()
        for f in records:
            key = str(f["case"])
            c = cases.get(key)
            if not c or key in seen:
                return refuse("FINDINGS_SCOPE")
            if f["actor"] != c["reviewer"] or f["assignment"] != c["assignment"]:
                return refuse("FINDING_ASSIGNMENT_CHANGED")
            if not active(self.s, f["actor"]) or f["mandate"] != self.s["actors"][f["actor"]]["revision"]:
                return refuse("STALE_AUTHORITY")
            if resume and f["basis"] != a["current"]:
                return refuse("STALE_EVIDENCE")
            if resume and f["seq"] != c.get("latest_finding"):
                return refuse("FINDING_SUPERSEDED")
            seen.add(key)
        if resume and seen != set(cases):
            return refuse("FINDINGS_INCOMPLETE")
        if resume:
            a["approval"] = cmd["approval"]
            a["used_approvals"].append(a["approval"]["id"])
        result = "RESUMED" if resume else "CANCELLED"
        event = self.log(result, "FRESH_APPROVAL" if resume else "OWNER_CANCELLED", **fields)
        a["decision"] = event["seq"]
        if resume:
            a["approval_seq"] = event["seq"]
        for c in cases.values():
            c.update(status="RESOLVED" if resume else "ACTION_CANCELLED", decision=event["seq"])
        a["status"] = "APPROVED" if resume else "CANCELLED"
        return event

    def commit(self):
        a, cmd, who = self.a, self.cmd, self.who
        key = canonical([who, self.aid, cmd["key"]])
        fingerprint = digest(cmd)
        old = self.s["commits"].get(key)
        if old:
            if old["digest"] == fingerprint:
                return event_at(self.db, old["seq"])
            return self.log("REFUSED", "COMMIT_KEY_CONFLICT", prior=old["seq"])
        result = self.try_commit()
        self.s["commits"][key] = dict(seq=result["seq"], digest=fingerprint)
        return result

    def try_commit(self):
        a, cmd, who = self.a, self.cmd, self.who
        if a is None:
            return self.log("REJECTED", "UNKNOWN_ACTION")
        p = a["approval"]
        fields = dict(key=cmd["key"], requested_approval=cmd["approval_id"], approval=a["approval_seq"],
                      target=cmd["target"], current=a["current"])
        if who != a["owner"]:
            return self.log("REFUSED", "OWNER_REQUIRED", **fields)
        if open_cases(a):
            return self.log("BLOCKED_HOLD", "OPEN_CHALLENGES", cases=sorted(map(int, open_cases(a))), **fields)
        if a["status"] != "APPROVED":
            return self.log("REFUSED", "ACTION_" + a["status"], **fields)
        if cmd["target"] != a["target"]:
            return self.log("REFUSED", "TARGET_MISMATCH", **fields)
        if cmd["approval_id"] != p["id"]:
            return self.log("REFUSED", "APPROVAL_MISMATCH", **fields)
        error = approval_error(self.s, a, p, set(p["reviewer_mandates"]))
        if error:
            return self.log("REFUSED", error, **fields)
        event = self.log("COMMITTED", "EFFECT_RECORDED", bound_approval=p, **fields)
        effect = dict(seq=event["seq"], gate=self.s["gate"], org=self.s["org"], action=self.aid,
                      target=a["target"], kind=a["effect"], tick=self.tick, approval=a["approval_seq"])
        self.db.execute("INSERT INTO effects VALUES (?,?,?)", (effect_action_key(self.aid), event["seq"], canonical(effect)))
        a.update(status="COMMITTED", commit_seq=event["seq"])
        return event


def submit(path, tick, principal, command, hook=None, timeout=15):
    """principal and tick come from trusted fixture adapters, separately from the delivery."""
    with closing(connect(path, timeout)) as db:
        db.execute("BEGIN IMMEDIATE")
        s = load_canonical(db.execute("SELECT body FROM local_state WHERE id=1").fetchone()[0])
        advance(db, s, tick)
        receipt = Transaction(db, s, tick, principal, command).dispatch()
        db.execute("UPDATE local_state SET body=? WHERE id=1", (canonical(s),))
        if hook:
            hook("before_commit")
        db.commit()
        if hook:
            hook("after_commit")
        return receipt
