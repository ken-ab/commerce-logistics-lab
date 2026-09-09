"""Local research endpoints; user confirmation is separate from agent tools."""
from contextlib import closing

from fastapi import Body, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from pydantic import Field
from typing import Literal

from apparel_fulfillment.orders import OrderError
from apparel_fulfillment.action_contract import TaskContract
from apparel_fulfillment.store import ApparelStore
from commerce_lab.state import Store, BusinessError
from delivery_budget import operational_ledger
from apparel_fulfillment.jobs import AgentJobs
from apparel_fulfillment.release import snapshot as study_snapshot


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewDraft(Payload):
    order: dict


class Selection(Payload):
    selections: list[dict]
    expected_revision: int


class Revision(Payload):
    expected_revision: int


class Approval(Revision):
    approval_id: str


class AgentTask(Payload):
    task: str = Field(min_length=1, max_length=4000)
    arm: Literal['single', 'coordinator', 'on_demand']
    operation: TaskContract | None = None


def install_apparel(app, *, operations=None, sessions=None, jobs=None):
    operations = operations or ApparelStore()
    sessions = sessions or Store()
    app.state.apparel_operations = operations
    jobs = jobs or AgentJobs(operations)
    app.state.apparel_jobs = jobs

    def owner(x_session_id: str = Header()):
        try:
            sessions.session(x_session_id)
        except BusinessError:
            raise HTTPException(401, "Create a valid local session") from None
        return x_session_id

    @app.exception_handler(OrderError)
    async def order_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get('/api/apparel/catalog')
    def catalog(session_id: str = Depends(owner)):
        with closing(operations.connect()) as db:
            world = operations._world(db)
        return {"dataset_id": world["dataset_id"], "warehouse": world["warehouse"], "notice": world["notice"],
                "styles": world.get("styles", {}), "variants": [{**variant, "stock": world["stock"][sku],
                    "brand_rule": world["brand_rules"].get(variant["brand"])} for sku, variant in world["variants"].items()]}

    @app.get('/api/apparel/status')
    def research_status(session_id: str = Depends(owner)):
        ledger = operational_ledger()
        policy = ledger.policy()
        return {"budget_limit_cny": min(480, policy['total_limit'], policy['automatic_spend_ceiling']),
                "budget": ledger.summary(), "dataset_id": operations.base_world['dataset_id'],
                "phase": study_snapshot()['status']}

    @app.get('/api/apparel/research')
    def research_results(session_id: str = Depends(owner)):
        return study_snapshot()

    @app.post('/api/apparel/drafts')
    def new_draft(payload: NewDraft, session_id: str = Depends(owner)):
        return operations.create_draft(session_id, payload.order)

    @app.get('/api/apparel/drafts/{draft_id}')
    def draft(draft_id: str, session_id: str = Depends(owner)):
        return operations.view(session_id, draft_id)

    @app.post('/api/apparel/drafts/{draft_id}/select')
    def select(draft_id: str, payload: Selection, session_id: str = Depends(owner)):
        return operations.select(session_id, draft_id, payload.selections, expected_revision=payload.expected_revision)

    @app.get('/api/apparel/drafts/{draft_id}/alternatives/{line_id}')
    def alternatives(draft_id: str, line_id: str, session_id: str = Depends(owner)):
        return operations.alternatives(session_id, draft_id, line_id)

    @app.post('/api/apparel/drafts/{draft_id}/approve-substitution')
    def approve(draft_id: str, payload: Approval, session_id: str = Depends(owner)):
        return operations.approve_substitution(session_id, draft_id, payload.approval_id, expected_revision=payload.expected_revision)

    @app.post('/api/apparel/drafts/{draft_id}/proposals')
    def propose(draft_id: str, payload: Revision, session_id: str = Depends(owner)):
        return operations.propose(session_id, draft_id, expected_revision=payload.expected_revision)

    @app.get('/api/apparel/drafts/{draft_id}/proposals/{proposal_id}/validity')
    def validity(draft_id: str, proposal_id: str, session_id: str = Depends(owner)):
        return operations.assess(session_id, draft_id, proposal_id)

    @app.post('/api/apparel/drafts/{draft_id}/proposals/{proposal_id}/confirm')
    def confirm(draft_id: str, proposal_id: str, session_id: str = Depends(owner)):
        return operations.confirm(session_id, draft_id, proposal_id)

    @app.get('/api/apparel/events')
    def events(session_id: str = Depends(owner)):
        return operations.transport_events()

    @app.post('/api/apparel/events')
    def add_event(event: dict = Body(), session_id: str = Depends(owner)):
        return operations.add_transport_event(event)

    @app.get('/api/apparel/drafts/{draft_id}/records')
    def records(draft_id: str, session_id: str = Depends(owner)):
        return {"draft": operations.view(session_id, draft_id), "traces": operations.traces(session_id, draft_id),
                "transport_events": operations.transport_events()}

    @app.post('/api/apparel/drafts/{draft_id}/agent')
    def start_agent(draft_id: str, payload: AgentTask, session_id: str = Depends(owner)):
        return jobs.start(session_id, draft_id, payload.task, payload.arm,
                          operation=payload.operation.model_dump() if payload.operation else None)

    @app.get('/api/apparel/runs/{job_id}')
    def agent_run(job_id: str, session_id: str = Depends(owner)):
        return jobs.get(session_id, job_id)

    @app.get('/api/apparel/drafts/{draft_id}/agent/latest')
    def latest_agent_run(draft_id: str, session_id: str = Depends(owner)):
        return jobs.latest(session_id, draft_id)

    @app.get('/api/apparel/runs/{job_id}/records')
    def agent_records(job_id: str, session_id: str = Depends(owner)):
        return jobs.get(session_id, job_id, full=True)

    return app
