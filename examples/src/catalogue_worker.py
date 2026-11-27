"""Single-store worker. stdin and stdout are the offline fixture transport."""
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

# Isolated Python does not automatically include the script's directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalogue
import gate
import trial_gate
from representation import load_canonical


def snapshot(path):
    # Read independently, through a connection that cannot write or create a store.
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('database integrity check failed')
        names = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {}
        for (name,) in names:
            if name not in ('local_state','journal','effects','decisions','listings'):
                raise ValueError('unexpected table')
            rows = [dict(r) for r in db.execute(f'SELECT * FROM {name} ORDER BY 1')]
            for r in rows:
                if 'body' in r:
                    r['body'] = load_canonical(r['body'])
            tables[name] = rows
        return tables


def main():
    path = Path(sys.argv[1])
    spec = json.load(sys.stdin)
    op = spec['op']
    args = spec.get('args', [])
    if op == 'snapshot':
        value = snapshot(path)
    elif op == 'init_gate':
        value = gate.initialize(path,*args)
    elif op == 'init_catalogue':
        value = catalogue.initialize(path)
    elif op in ('issue','intake','reconcile'):
        value = trial_gate.write(path,spec['tick'],getattr(trial_gate,op),*args)
    elif op == 'block_message':
        value = trial_gate.block_message(path,*args)
    elif op in ('post','block'):
        value = catalogue.apply(path,op,*args)
        if spec.get('exit_before_reply'):
            os._exit(74)  # Tx committed. No reply frame has been emitted.
    elif op == 'lookup':
        value = catalogue.lookup(path,*args)
    else:
        raise ValueError('unknown operation')
    print(json.dumps(value,sort_keys=True))


if __name__ == '__main__':
    main()
