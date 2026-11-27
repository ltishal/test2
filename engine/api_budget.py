"""Persistent fail-closed daily guard for outbound model attempts (UTC)."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

class BudgetExceeded(RuntimeError):
    pass

class ApiBudget:
    def __init__(self,path,global_calls=3000,owner_calls=150,global_units=30000000):
        self.path=path
        self.global_calls=global_calls
        self.owner_calls=owner_calls
        self.global_units=global_units
        with closing(sqlite3.connect(path,timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS api_budget(day TEXT, scope TEXT, calls INTEGER NOT NULL, units INTEGER NOT NULL, PRIMARY KEY(day,scope))')

    def reserve(self,owner,units,day=None):
        day=day or datetime.now(timezone.utc).date().isoformat()
        units=max(1,int(units))
        scopes=[('global',self.global_calls,self.global_units)]
        if owner: scopes.append(('owner:'+owner,self.owner_calls,None))
        with closing(sqlite3.connect(self.path,timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            for scope,limit,unit_limit in scopes:
                row=db.execute('SELECT calls,units FROM api_budget WHERE day=? AND scope=?',(day,scope)).fetchone() or (0,0)
                if row[0]>=limit or (unit_limit is not None and row[1]+units>unit_limit):
                    raise BudgetExceeded('Дневной лимит обращений к ИИ исчерпан. Попробуйте позже.')
                db.execute('INSERT INTO api_budget VALUES (?,?,1,?) ON CONFLICT(day,scope) DO UPDATE SET calls=calls+1, units=units+excluded.units',(day,scope,units))


def estimated_units(options):
    # Conservative character-based proxy, NOT tokenizer/billing exact.
    extra=options.get('extra_body') or {}
    limit=extra.get('max_completion_tokens') or options.get('max_completion_tokens') or options.get('max_tokens') or 4096
    return sum(len(str(m.get('content','')).encode('utf-8')) for m in options.get('messages',[]) if isinstance(m,dict))+int(limit)
