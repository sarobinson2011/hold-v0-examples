"""One process, one store and one transport envelope per call."""
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate
import mandates
import trial_gate
from representation import load_canonical


def inspect(path):
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('integrity check failed')
        schema = [dict(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name")]
        tables = {}
        for spec in schema:
            name = spec['name']
            if name not in ('authority','journal','changes','decisions','listings','effects','local_state'):
                raise ValueError('unexpected table')
            rows = [dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY 1')]
            for r in rows:
                for k in ('body','config'):
                    if k in r:
                        r[k] = load_canonical(r[k])
            tables[name] = rows
        return dict(schema=schema,tables=tables)


def main():
    path = Path(sys.argv[1])
    msg = json.load(sys.stdin)
    op, args = msg['op'], msg['args']
    if op == 'inspect':
        value = inspect(path)
    elif op == 'init_gate':
        value = gate.initialize(path,*args)
    elif op == 'init_catalogue':
        value = mandates.init_catalogue(path,*args)
    elif op in ('gate_issue','gate_revoke','reconcile'):
        value = trial_gate.write(path,msg['tick'],getattr(mandates,op),*args)
    elif op == 'read_notice':
        with closing(gate.connect(path)) as db:
            s = load_canonical(db.execute('SELECT body FROM local_state WHERE id=1').fetchone()[0])
            value = s['actions'][args[0]]['revocation']
    elif op == 'lookup':
        value = mandates.lookup(path,*args)
    elif op in ('post','notice'):
        value = mandates.catalogue_write(path,op,args)
        if msg.get('lose_reply'):
            os._exit(74)  # After COMMIT, before any bytes are emitted.
    else:
        raise ValueError('unknown worker command')
    print(json.dumps(value,sort_keys=True))


if __name__ == '__main__':
    main()
