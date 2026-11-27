"""Finite command grammar. A fixture principal and trusted receipt tick travel separately."""
CLAIM = dict(type=str, issuer=str, id=str, version=str, digest=str)
BASIS = dict(claim=CLAIM, evidence=str)
APPROVAL = dict(id=str, target=str, **BASIS, owner_mandate=int, reviewer_mandates={"*": int})
CORE = dict(challenger=str, id=str, claim=CLAIM, ground=str)
FIELDS = {
    "deliver": dict(action=str, org=str, challenge=CORE, ref=str),
    "advance": {},
    "mandate": dict(subject=str, revision=int, active=bool),
    "commit": dict(action=str, target=str, key=str, approval_id=str),
    "prepare": dict(action=str),
    "update_basis": dict(action=str, basis=BASIS),
    "petition": dict(action=str, case=int),
    "escalate": dict(action=str, case=int, reason=str),
    "finding": dict(action=str, case=int, basis=BASIS, mandate=int, ref=str),
    "resume": dict(action=str, findings=[int], owner_mandate=int, reason=str, approval=APPROVAL),
    "cancel": dict(action=str, findings=[int], owner_mandate=int, reason=str)
}
OPTIONAL = {"deliver": {"sent_tick":int}, "escalate": {"replacement":str}}


def matches(value, shape):
    if isinstance(shape, type):
        return type(value) is shape
    if isinstance(shape, list):
        return isinstance(value, list) and all(matches(v, shape[0]) for v in value)
    if not isinstance(value, dict):
        return False
    if "*" in shape:
        return all(isinstance(k, str) and matches(v, shape["*"]) for k, v in value.items())
    return value.keys() == shape.keys() and all(matches(value[k], spec) for k, spec in shape.items())


def valid(command):
    if not isinstance(command, dict) or not isinstance(command.get("op"), str):
        return False
    op = command["op"]
    if op not in FIELDS:
        return False
    shape = dict(op=str, **FIELDS[op])
    shape.update({k:v for k,v in OPTIONAL.get(op, {}).items() if k in command})
    if not matches(command, shape):
        return False
    if op == "deliver":
        return all(command["challenge"][k].strip() for k in ("id", "challenger"))
    return True
