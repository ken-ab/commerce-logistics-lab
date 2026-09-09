"""Versioned prompt skills; candidates cannot replace tools, guards or evaluators."""
from contextlib import closing
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from commerce_lab.state import BusinessError, now


class SkillPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,79}$')
    search_match_mode: Literal['all', 'any'] = 'all'
    retry_empty_search: bool = False
    pre_read_stock: bool = False
    role_guidance: dict[Literal['coordinator', 'catalog', 'logistics', 'service'], str] = Field(default_factory=dict)

    def checked(self):
        if any(not text.strip() or len(text) > 1400 for text in self.role_guidance.values()):
            raise ValueError('Each role instruction must contain 1 to 1400 characters')
        return self.model_dump()


BASELINE = SkillPolicy(id='baseline-v1').checked()


class SkillRegistry:
    def __init__(self, store):
        self.store = store
        with closing(store.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS skill_events(id INTEGER PRIMARY KEY,kind TEXT,skill_id TEXT,evidence TEXT,created_at TEXT)')
            db.execute('INSERT OR IGNORE INTO skills VALUES (?,?,?,?,?,?)',
                ('baseline-v1', 1, 'active', json.dumps(BASELINE), '{}', now()))
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_skill ON skills(status) WHERE status='active'")

    def active(self):
        with closing(self.store.connect()) as db:
            row = db.execute("SELECT config FROM skills WHERE status='active'").fetchone()
        if not row:
            raise BusinessError('No active skill; explicit recovery is required')
        return SkillPolicy.model_validate_json(row['config']).checked()

    def register(self, policy, evidence):
        policy = SkillPolicy.model_validate(policy).checked()
        encoded = json.dumps(policy, sort_keys=True)
        with closing(self.store.connect()) as db, db:
            existing = db.execute('SELECT config FROM skills WHERE id=?', (policy['id'],)).fetchone()
            if existing:
                if SkillPolicy.model_validate_json(existing['config']).checked() != policy:
                    raise BusinessError('A registered skill ID is immutable')
                return
            version = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM skills').fetchone()[0]
            db.execute('INSERT INTO skills VALUES (?,?,?,?,?,?)',
                (policy['id'], version, 'candidate', encoded, json.dumps(evidence), now()))

    def activate(self, ident, *, expected_parent, evidence, rollback=False):
        # Callers must first verify the held-out gate package. This transaction
        # prevents concurrent/stale promotions from overwriting a later decision.
        with closing(self.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute("SELECT id FROM skills WHERE status='active'").fetchone()
            if not current or current['id'] != expected_parent:
                raise BusinessError('Active skill changed; refusing a stale transition')
            target = db.execute('SELECT status FROM skills WHERE id=?', (ident,)).fetchone()
            allowed = {'retired'} if rollback else {'candidate'}
            if not target or target['status'] not in allowed:
                raise BusinessError('Target skill is not eligible for this transition')
            db.execute("UPDATE skills SET status='retired' WHERE id=?", (expected_parent,))
            db.execute("UPDATE skills SET status='active' WHERE id=?", (ident,))
            db.execute('INSERT INTO skill_events(kind,skill_id,evidence,created_at) VALUES (?,?,?,?)',
                ('rollback' if rollback else 'promotion', ident, json.dumps(evidence), now()))
