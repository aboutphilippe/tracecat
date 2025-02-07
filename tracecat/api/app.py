import json
from contextlib import asynccontextmanager
from typing import Annotated, Any

import polars as pl
import tantivy
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.params import Body
from fastapi.responses import StreamingResponse
from sqlalchemy import Engine, or_
from sqlalchemy.exc import NoResultFound
from sqlmodel import Session, select

from tracecat.api.completions import CategoryConstraint, stream_case_completions
from tracecat.auth import (
    AuthenticatedRunnerClient,
    Role,
    authenticate_service,
    authenticate_user,
    authenticate_user_or_service,
)
from tracecat.config import TRACECAT__APP_ENV, TRACECAT__RUNNER_URL
from tracecat.db import (
    Action,
    ActionRun,
    CaseAction,
    CaseContext,
    Secret,
    User,
    Webhook,
    Workflow,
    WorkflowRun,
    clone_workflow,
    create_events_index,
    create_vdb_conn,
    initialize_db,
)
from tracecat.logger import standard_logger

# TODO: Clean up API params / response "zoo"
# lots of repetition and inconsistency
from tracecat.types.api import (
    ActionMetadataResponse,
    ActionResponse,
    ActionRunResponse,
    AuthenticateWebhookResponse,
    CaseActionParams,
    CaseContextParams,
    CaseParams,
    CopyWorkflowParams,
    CreateActionParams,
    CreateActionRunParams,
    CreateSecretParams,
    CreateWebhookParams,
    CreateWorkflowParams,
    Event,
    EventSearchParams,
    SearchSecretsParams,
    SearchWebhooksParams,
    StartWorkflowParams,
    StartWorkflowResponse,
    TriggerWorkflowRunParams,
    UpdateActionParams,
    UpdateActionRunParams,
    UpdateSecretParams,
    UpdateUserParams,
    UpdateWorkflowParams,
    UpdateWorkflowRunParams,
    WebhookResponse,
    WorkflowMetadataResponse,
    WorkflowResponse,
    WorkflowRunResponse,
)
from tracecat.types.cases import Case, CaseMetrics

logger = standard_logger("api")


engine: Engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = initialize_db()
    yield


app = FastAPI(lifespan=lifespan)

if TRACECAT__APP_ENV == "prod":
    # NOTE: If you are using Tracecat self-hosted
    # please replace with your own domain
    origins = ["https://platform.tracecat.com", TRACECAT__RUNNER_URL]
elif TRACECAT__APP_ENV == "staging":
    origins = ["https://tracecat-*-tracecat.vercel.app", TRACECAT__RUNNER_URL]
else:
    origins = [
        "http://localhost:3000",
        "http://localhost:8000",
    ]

# TODO: Check TRACECAT__APP_ENV to set methods and headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root() -> dict[str, str]:
    return {"message": "Hello world. I am the API."}


@app.get("/health")
def check_health() -> dict[str, str]:
    return {"message": "Hello world. I am the API. This is the health endpoint."}


### Workflows


@app.get("/workflows")
def list_workflows(
    role: Annotated[Role, Depends(authenticate_user)],
    library: bool = False,
) -> list[WorkflowMetadataResponse]:
    """List all Workflows in database."""
    query_user_id = role.user_id if not library else "tracecat"
    with Session(engine) as session:
        statement = select(Workflow).where(Workflow.owner_id == query_user_id)
        results = session.exec(statement)
        workflows = results.all()
    workflow_metadata = [
        WorkflowMetadataResponse(
            id=workflow.id,
            title=workflow.title,
            description=workflow.description,
            status=workflow.status,
            icon_url=workflow.icon_url,
        )
        for workflow in workflows
    ]
    return workflow_metadata


@app.post("/workflows", status_code=status.HTTP_201_CREATED)
def create_workflow(
    role: Annotated[Role, Depends(authenticate_user)],
    params: CreateWorkflowParams,
) -> WorkflowMetadataResponse:
    """Create new Workflow with title and description."""
    workflow = Workflow(
        title=params.title,
        description=params.description,
        owner_id=role.user_id,
    )
    with Session(engine) as session:
        session.add(workflow)
        session.commit()
        session.refresh(workflow)

    return WorkflowMetadataResponse(
        id=workflow.id,
        title=workflow.title,
        description=workflow.description,
        status=workflow.status,
        icon_url=workflow.icon_url,
    )


@app.get("/workflows/{workflow_id}")
def get_workflow(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
) -> WorkflowResponse:
    """Return Workflow as title, description, list of Action JSONs, adjacency list of Action IDs."""
    with Session(engine) as session:
        # Get Workflow given workflow_id
        statement = select(Workflow).where(
            Workflow.owner_id == role.user_id,
            Workflow.id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        # List all Actions related to `workflow_id`
        statement = select(Action).where(Action.workflow_id == workflow_id)
        results = session.exec(statement)
        actions = results.all()

        object = None
        if workflow.object is not None:
            # Process react flow object into adjacency list
            object = json.loads(workflow.object)

    actions_responses = {
        action.id: ActionResponse(
            id=action.id,
            type=action.type,
            title=action.title,
            description=action.description,
            status=action.status,
            inputs=json.loads(action.inputs) if action.inputs else None,
            key=action.key,
        )
        for action in actions
    }
    workflow_response = WorkflowResponse(
        id=workflow.id,
        title=workflow.title,
        description=workflow.description,
        status=workflow.status,
        actions=actions_responses,
        object=object,
        owner_id=workflow.owner_id,
    )
    return workflow_response


@app.post("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def update_workflow(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    params: UpdateWorkflowParams,
) -> None:
    """Update Workflow."""

    with Session(engine) as session:
        statement = select(Workflow).where(
            Workflow.owner_id == role.user_id,
            Workflow.id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        if params.title is not None:
            workflow.title = params.title
        if params.description is not None:
            workflow.description = params.description
        if params.status is not None:
            workflow.status = params.status
        if params.object is not None:
            workflow.object = params.object

        session.add(workflow)
        session.commit()


@app.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
) -> None:
    """Delete Workflow."""

    with Session(engine) as session:
        statement = select(Workflow).where(
            Workflow.owner_id == role.user_id,
            Workflow.id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        session.delete(workflow)
        session.commit()


@app.post("/workflows/{workflow_id}/copy", status_code=status.HTTP_204_NO_CONTENT)
def copy_workflow(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    workflow_id: str,
    params: Annotated[CopyWorkflowParams | None, Body(...)] = None,
) -> None:
    """Copy a Workflow.

    We currently only permit copying workflows from the tracecat user into the user's own account.
    """
    if role.type == "user" and params is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Users can only clone tracecat workflows",
        )
    # Users cannot pass owner_id, and defaults to 'tracecat'
    owner_id = params.owner_id if params else "tracecat"
    with Session(engine) as session:
        statement = select(Workflow).where(
            Workflow.owner_id == owner_id,
            Workflow.id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        # Assign it a new workflow ID and owner ID
        new_workflow = clone_workflow(workflow, session, role.user_id)
        session.commit()
        session.refresh(new_workflow)


### Workflow Runs


@app.get("/workflows/{workflow_id}/runs")
def list_workflow_runs(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    limit: int = 20,
) -> list[WorkflowRunResponse]:
    """List all Workflow Runs for a Workflow."""
    with Session(engine) as session:
        # Being here means the user has access to the workflow
        statement = (
            select(WorkflowRun)
            .where(
                WorkflowRun.owner_id == role.user_id,
                WorkflowRun.workflow_id == workflow_id,
            )
            .limit(limit)
        )
        results = session.exec(statement)
        workflow_runs = results.all()

    workflow_runs_metadata = [
        WorkflowRunResponse(**workflow_run.model_dump())
        for workflow_run in workflow_runs
    ]
    return workflow_runs_metadata


@app.post("/workflows/{workflow_id}/runs", status_code=status.HTTP_201_CREATED)
def create_workflow_run(
    role: Annotated[Role, Depends(authenticate_service)],  # M2M
    workflow_id: str,
) -> WorkflowRunResponse:
    """Create a Workflow Run."""

    with Session(engine) as session:
        workflow_run = WorkflowRun(owner_id=role.user_id, workflow_id=workflow_id)
        session.add(workflow_run)
        session.commit()
        session.refresh(workflow_run)
        # Need this classmethod to instantiate the lazy-laoded action_runs list
        return WorkflowRunResponse.from_orm(workflow_run)


@app.get("/workflows/{workflow_id}/runs/{workflow_run_id}")
def get_workflow_run(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    workflow_run_id: str,
) -> WorkflowRunResponse:
    """Return WorkflowRun as title, description, list of Action JSONs, adjacency list of Action IDs."""

    with Session(engine) as session:
        # Get Workflow given workflow_id
        statement = select(WorkflowRun).where(
            WorkflowRun.owner_id == role.user_id,
            WorkflowRun.id == workflow_run_id,
            WorkflowRun.workflow_id == workflow_id,  # Redundant, but for clarity
        )
        result = session.exec(statement)
        try:
            workflow_run = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        # Need this classmethod to instantiate the lazy-laoded action_runs list
        return WorkflowRunResponse.from_orm(workflow_run)


@app.post(
    "/workflows/{workflow_id}/runs/{workflow_run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def update_workflow_run(
    role: Annotated[Role, Depends(authenticate_service)],  # M2M
    workflow_id: str,
    workflow_run_id: str,
    params: UpdateWorkflowRunParams,
) -> None:
    """Update Workflow."""

    with Session(engine) as session:
        statement = select(WorkflowRun).where(
            WorkflowRun.owner_id == role.user_id,
            WorkflowRun.id == workflow_run_id,
            WorkflowRun.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        try:
            workflow_run = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        if params.status is not None:
            workflow_run.status = params.status

        session.add(workflow_run)
        session.commit()


@app.post("/workflows/{workflow_id}/trigger")
async def trigger_workflow_run(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    params: TriggerWorkflowRunParams,
) -> StartWorkflowResponse:
    """Trigger a Workflow Run."""
    # Create service role
    service_role = Role(type="service", user_id=role.user_id, service_id="tracecat-api")
    workflow_params = StartWorkflowParams(
        entrypoint_key=params.action_key,
        entrypoint_payload=params.payload,
    )
    logger.debug(f"Triggering workflow: {workflow_id = }, {workflow_params = }")
    async with AuthenticatedRunnerClient(role=service_role, http2=True) as client:
        response = await client.post(
            f"/workflows/{workflow_id}",
            json=workflow_params.model_dump(),
        )
        try:
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Error triggering workflow: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error triggering workflow",
            ) from e

    return response.json()


### Actions


@app.get("/actions")
def list_actions(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
) -> list[ActionMetadataResponse]:
    """List all Actions related to `workflow_id`."""
    with Session(engine) as session:
        statement = select(Action).where(
            Action.owner_id == role.user_id,
            Action.workflow_id == workflow_id,
        )
        results = session.exec(statement)
        actions = results.all()
    action_metadata = [
        ActionMetadataResponse(
            id=action.id,
            workflow_id=workflow_id,
            type=action.type,
            title=action.title,
            description=action.description,
            status=action.status,
            key=action.key,
        )
        for action in actions
    ]
    return action_metadata


@app.post("/actions")
def create_action(
    role: Annotated[Role, Depends(authenticate_user)],
    params: CreateActionParams,
) -> ActionMetadataResponse:
    with Session(engine) as session:
        action = Action(
            owner_id=role.user_id,
            workflow_id=params.workflow_id,
            type=params.type,
            title=params.title,
            description="",  # Default to empty string
        )
        session.add(action)
        session.commit()
        session.refresh(action)

        if params.type.lower() == "webhook":
            create_webhook(
                role=role,
                params=CreateWebhookParams(
                    action_id=action.id, workflow_id=params.workflow_id
                ),
            )
    action_metadata = ActionMetadataResponse(
        id=action.id,
        workflow_id=params.workflow_id,
        type=params.type,
        title=action.title,
        description=action.description,
        status=action.status,
        key=action.key,
    )
    return action_metadata


@app.get("/actions/{action_id}")
def get_action(
    role: Annotated[Role, Depends(authenticate_user)],
    action_id: str,
    workflow_id: str,
) -> ActionResponse:
    with Session(engine) as session:
        statement = select(Action).where(
            Action.owner_id == role.user_id,
            Action.id == action_id,
            Action.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        try:
            action = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        inputs: dict[str, Any] = json.loads(action.inputs) if action.inputs else {}
        # Precompute webhook response
        # Alias webhook.id as path
        if action.type.lower() == "webhook":
            webhook = search_webhooks(
                role=role,
                params=SearchWebhooksParams(action_id=action.id),
            )
            inputs.update(path=webhook.id, secret=webhook.secret, url=webhook.url)
    return ActionResponse(
        id=action.id,
        type=action.type,
        title=action.title,
        description=action.description,
        status=action.status,
        inputs=None if len(inputs) == 0 else inputs,
        key=action.key,
    )


@app.post("/actions/{action_id}")
def update_action(
    role: Annotated[Role, Depends(authenticate_user)],
    action_id: str,
    params: UpdateActionParams,
) -> ActionResponse:
    with Session(engine) as session:
        # Fetch the action by id
        statement = select(Action).where(
            Action.owner_id == role.user_id,
            Action.id == action_id,
        )
        result = session.exec(statement)
        try:
            action = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        if params.title is not None:
            action.title = params.title
        if params.description is not None:
            action.description = params.description
        if params.status is not None:
            action.status = params.status
        if params.inputs is not None:
            action.inputs = params.inputs

        session.add(action)
        session.commit()
        session.refresh(action)

    return ActionResponse(
        id=action.id,
        type=action.type,
        title=action.title,
        description=action.description,
        status=action.status,
        inputs=json.loads(action.inputs) if action.inputs else None,
        key=action.key,
    )


@app.delete("/actions/{action_id}", status_code=204)
def delete_action(
    role: Annotated[Role, Depends(authenticate_user)],
    action_id: str,
) -> None:
    with Session(engine) as session:
        statement = select(Action).where(
            Action.owner_id == role.user_id,
            Action.id == action_id,
        )
        result = session.exec(statement)
        try:
            action = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        # If the user doesn't own this workflow, they can't delete the action
        session.delete(action)
        session.commit()


### Action Runs


@app.get("/actions/{action_id}/runs")
def list_action_runs(
    role: Annotated[Role, Depends(authenticate_user)],
    action_id: str,
    limit: int = 20,
) -> list[ActionRunResponse]:
    """List all action Runs for an action."""
    with Session(engine) as session:
        # Being here means the user has access to the action
        statement = (
            select(ActionRun)
            .where(
                ActionRun.owner_id == role.user_id,
                ActionRun.action_id == action_id,
            )
            .limit(limit)
        )
        results = session.exec(statement)
        action_runs = results.all()

    action_runs_metadata = [
        ActionRunResponse(**action_run.model_dump()) for action_run in action_runs
    ]
    return action_runs_metadata


@app.post("/actions/{action_id}/runs", status_code=status.HTTP_201_CREATED)
def create_action_run(
    role: Annotated[Role, Depends(authenticate_service)],  # M2M
    action_id: str,
    params: CreateActionRunParams,
) -> ActionRunResponse:
    """Create a action Run."""

    action_run = ActionRun(
        owner_id=role.user_id,
        action_id=action_id,
        id=params.action_run_id,
        workflow_run_id=params.workflow_run_id,
    )
    with Session(engine) as session:
        session.add(action_run)
        session.commit()
        session.refresh(action_run)

    return ActionRunResponse(**action_run.model_dump())


@app.get("/actions/{action_id}/runs/{action_run_id}")
def get_action_run(
    role: Annotated[Role, Depends(authenticate_user)],
    action_id: str,
    action_run_id: str,
) -> ActionRunResponse:
    """Return ActionRun as title, description, of Action JSONs, adjacency list of Action IDs."""

    with Session(engine) as session:
        # Get action given action_id
        statement = select(ActionRun).where(
            ActionRun.owner_id == role.user_id,
            ActionRun.id == action_run_id,
            ActionRun.action_id == action_id,  # Redundant, but for clarity
        )
        result = session.exec(statement)
        try:
            action_run = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

    return ActionRunResponse(**action_run.model_dump())


@app.post(
    "/actions/{action_id}/runs/{action_run_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def update_action_run(
    role: Annotated[Role, Depends(authenticate_service)],  # M2M
    action_id: str,
    action_run_id: str,
    params: UpdateActionRunParams,
) -> None:
    """Update action."""

    with Session(engine) as session:
        statement = select(ActionRun).where(
            ActionRun.owner_id == role.user_id,
            ActionRun.id == action_run_id,
            ActionRun.action_id == action_id,
        )
        result = session.exec(statement)
        try:
            action_run = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e

        if params.status is not None:
            action_run.status = params.status

        session.add(action_run)
        session.commit()


### Webhooks


@app.get("/webhooks")
def list_webhooks(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
) -> list[WebhookResponse]:
    """List all Webhooks for a workflow."""
    with Session(engine) as session:
        statement = select(Webhook).where(
            Webhook.owner_id == role.user_id,
            Webhook.workflow_id == workflow_id,
        )
        result = session.exec(statement)
        webhooks = result.all()
    webhook_responses = [
        WebhookResponse(
            id=webhook.id,
            path=webhook.path,
            action_id=webhook.action_id,
            workflow_id=webhook.workflow_id,
            url=webhook.url,
        )
        for webhook in webhooks
    ]
    return webhook_responses


@app.post("/webhooks", status_code=status.HTTP_201_CREATED)
def create_webhook(
    role: Annotated[Role, Depends(authenticate_user)],
    params: CreateWebhookParams,
) -> WebhookResponse:
    """Create a new Webhook."""
    webhook = Webhook(
        owner_id=role.user_id,
        action_id=params.action_id,
        workflow_id=params.workflow_id,
    )
    with Session(engine) as session:
        session.add(webhook)
        session.commit()
        session.refresh(webhook)

    return WebhookResponse(
        id=webhook.id,
        action_id=webhook.action_id,
        workflow_id=webhook.workflow_id,
        secret=webhook.secret,
        url=webhook.url,
    )


@app.get("/webhooks/{webhook_id}")
def get_webhook(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    webhook_id: str,
) -> WebhookResponse:
    with Session(engine) as session:
        statement = select(Webhook).where(
            Webhook.owner_id == role.user_id,
            Webhook.id == webhook_id,
        )
        result = session.exec(statement)
        try:
            webhook = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
    webhook_response = WebhookResponse(
        id=webhook.id,
        secret=webhook.secret,
        action_id=webhook.action_id,
        workflow_id=webhook.workflow_id,
        url=webhook.url,
    )
    return webhook_response


@app.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(
    role: Annotated[Role, Depends(authenticate_user)],
    webhook_id: str,
) -> None:
    """Delete a Webhook by ID."""
    with Session(engine) as session:
        statement = select(Webhook).where(
            Webhook.owner_id == role.user_id,
            Webhook.id == webhook_id,
        )
        result = session.exec(statement)
        webhook = result.one()
        session.delete(webhook)
        session.commit()


@app.get("/webhooks/search")
def search_webhooks(
    role: Annotated[Role, Depends(authenticate_user)],
    params: SearchWebhooksParams,
) -> WebhookResponse:
    with Session(engine) as session:
        statement = select(Webhook)

        if params.action_id is not None:
            statement = statement.where(
                Webhook.owner_id == role.user_id,
                Webhook.action_id == params.action_id,
            )
        result = session.exec(statement)
        try:
            webhook = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
    webhook_response = WebhookResponse(
        id=webhook.id,
        secret=webhook.secret,
        action_id=webhook.action_id,
        workflow_id=webhook.workflow_id,
        url=webhook.url,
    )
    return webhook_response


@app.post("/authenticate/webhooks/{webhook_id}/{secret}")
def authenticate_webhook(
    # TODO: Add user id to Role
    _role: Annotated[Role, Depends(authenticate_service)],  # M2M
    webhook_id: str,
    secret: str,
) -> AuthenticateWebhookResponse:
    with Session(engine) as session:
        statement = select(Webhook).where(Webhook.id == webhook_id)
        result = session.exec(statement)
        try:
            webhook = result.one()
        except NoResultFound as e:
            logger.error("Webhook does not exist: %s", e)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        if webhook.secret != secret:
            logger.error("Secret doesn't match")
            return AuthenticateWebhookResponse(status="Unauthorized")
        # Get slug
        statement = select(Action).where(Action.id == webhook.action_id)
        result = session.exec(statement)
        try:
            action = result.one()
        except Exception as e:
            logger.error("Action does not exist: %s", e)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
    return AuthenticateWebhookResponse(
        status="Authorized",
        owner_id=action.owner_id,
        action_id=action.id,
        action_key=action.key,
        workflow_id=webhook.workflow_id,
        webhook_id=webhook_id,
    )


### Events Management


SUPPORTED_EVENT_AGGS = {
    "count": pl.count,
    "max": pl.max,
    "avg": pl.mean,
    "median": pl.median,
    "min": pl.min,
    "std": pl.std,
    "sum": pl.sum,
    "var": pl.var,
}


@app.get("/events/search")
def search_events(
    role: Annotated[Role, Depends(authenticate_user)],
    params: EventSearchParams,
) -> list[Event]:
    """Search for events based on query parameters.

    Note: currently on supports filter by `workflow_id` and sort by `published_at`.
    """
    # TODO: Change this to use tantivy filtered query
    with Session(engine) as session:
        statement = select(Workflow.owner_id).where(Workflow.id == params.workflow_id)
        workflow_owner_id = session.exec(statement).one_or_none()
        if not workflow_owner_id or workflow_owner_id != role.user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden"
            )
    index = create_events_index()
    index.reload()
    query = index.parse_query(params.workflow_id, ["workflow_id"])
    searcher = index.searcher()
    searcher.search(
        query, order_by_field=tantivy.field(params.order_by), limit=params.limit
    )


### Case Management


@app.post("/workflows/{workflow_id}/cases", status_code=status.HTTP_201_CREATED)
def create_case(
    role: Annotated[Role, Depends(authenticate_service)],  # M2M
    workflow_id: str,
    cases: list[CaseParams],
):
    db = create_vdb_conn()
    tbl = db.open_table("cases")
    # Should probably also add a check for existing case IDs
    new_cases = [
        Case(**c.model_dump(), owner_id=role.user_id, workflow_id=workflow_id)
        for c in cases
    ]
    tbl.add([case.flatten() for case in new_cases])


@app.get("/workflows/{workflow_id}/cases")
def list_cases(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    limit: int = 100,
) -> list[Case]:
    """List all cases under a workflow.

    Note: currently only supports listing the first 100 cases.
    """
    db = create_vdb_conn()
    tbl = db.open_table("cases")
    result = (
        tbl.search()
        .where(f"(owner_id = {role.user_id!r}) AND (workflow_id = {workflow_id!r})")
        .select(list(Case.model_fields.keys()))
        .limit(limit)
        .to_polars()
        .to_dicts()
    )
    return [Case.from_flattened(c) for c in result]


@app.get("/workflows/{workflow_id}/cases/{case_id}")
def get_case(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    case_id: str,
) -> Case:
    """Get a specific case by ID under a workflow."""
    db = create_vdb_conn()
    tbl = db.open_table("cases")
    result = (
        tbl.search()
        .where(
            f"(owner_id = {role.user_id!r}) AND (workflow_id = {workflow_id!r}) AND (id = {case_id!r})"
        )
        .select(list(Case.model_fields.keys()))
        .limit(1)
        .to_polars()
        .to_dicts()
    )
    return [Case.from_flattened(c) for c in result]


@app.post("/workflows/{workflow_id}/cases/{case_id}")
def update_case(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    case_id: str,
    params: CaseParams,
):
    """Update a specific case by ID under a workflow."""
    updated_case = Case.from_params(params, owner_id=role.user_id, id=case_id)
    db = create_vdb_conn()
    tbl = db.open_table("cases")
    tbl.update(
        where=f"(owner_id = {role.user_id!r}) AND (workflow_id = {workflow_id!r}) AND (id = {case_id!r})",
        values=updated_case.flatten(),
    )


@app.get("/workflows/{workflow_id}/cases/{case_id}/metrics")
def get_case_metrics(
    role: Annotated[Role, Depends(authenticate_user)],
    workflow_id: str,
    case_id: str,
) -> CaseMetrics:
    """Get a specific case by ID under a workflow."""
    db = create_vdb_conn()
    tbl = db.open_table("cases")
    df = pl.DataFrame(
        tbl.search()
        .where(
            f"(owner_id = {role.user_id!r}) AND (workflow_id = {workflow_id!r}) AND (id = {case_id!r})"
        )
        .select(list(Case.model_fields.keys()))
        .to_arrow()
    ).to_dicts()
    return df


### Available Case Actions


@app.get("/case-actions")
def list_case_actions(
    role: Annotated[Role, Depends(authenticate_user)],
) -> list[CaseAction]:
    with Session(engine) as session:
        statement = select(CaseAction).where(
            or_(
                CaseAction.owner_id == "tracecat",
                CaseAction.owner_id == role.user_id,
            )
        )
        actions = session.exec(statement).all()
    return actions


@app.post("/case-actions")
def create_case_action(
    role: Annotated[Role, Depends(authenticate_user)],
    params: CaseActionParams,
) -> CaseAction:
    case_action = CaseAction(owner_id=role.user_id, **params.model_dump())
    with Session(engine) as session:
        session.add(case_action)
        session.commit()
        session.refresh(case_action)
    return case_action


@app.delete("/case-actions/{case_action_id}")
def delete_case_action(
    role: Annotated[Role, Depends(authenticate_user)],
    case_action_id: str,
):
    with Session(engine) as session:
        statement = select(CaseAction).where(
            CaseAction.owner_id == role.user_id,
            CaseAction.id == case_action_id,
        )
        result = session.exec(statement)
        try:
            action = result.one()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        session.delete(action)
        session.commit()


### Available Context Labels


@app.get("/case-contexts")
def list_case_contexts(
    role: Annotated[Role, Depends(authenticate_user)],
) -> list[CaseContext]:
    with Session(engine) as session:
        statement = select(CaseContext).where(
            or_(
                CaseContext.owner_id == "tracecat",
                CaseContext.owner_id == role.user_id,
            )
        )
        actions = session.exec(statement).all()
    return actions


@app.post("/case-contexts")
def create_case_context(
    role: Annotated[Role, Depends(authenticate_user)],
    params: CaseContextParams,
) -> CaseContext:
    case_context = CaseContext(owner_id=role.user_id, **params.model_dump())
    with Session(engine) as session:
        session.add(case_context)
        session.commit()
        session.refresh(case_context)
    return params


@app.delete("/case-contexts/{case_context_id}")
def delete_case_context(
    role: Annotated[Role, Depends(authenticate_user)],
    case_context_id: str,
):
    with Session(engine) as session:
        statement = select(CaseContext).where(
            CaseContext.owner_id == role.user_id,
            CaseContext.id == case_context_id,
        )
        result = session.exec(statement)
        try:
            action = result.one()
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
            ) from e
        session.delete(action)
        session.delete(action)
        session.commit()
    pass


@app.post("/completions/cases/stream")
async def streaming_autofill_case_fields(
    role: Annotated[Role, Depends(authenticate_user)],
    cases: list[Case],  # TODO: Replace this with case IDs
) -> dict[str, str]:
    """List of case IDs.
    Steps
    -----
    1. Using Case IDs, fetch case data
    2. Figure out  which fields need to be populated - these fields are None
    3. Complete the fields

    """
    logger.info(f"Received cases: {cases = }, {role = }")
    case_actions = list_case_actions(role)
    actions_mapping = (
        pl.DataFrame(case_actions)
        .lazy()
        .select(
            pl.col.tag,
            pl.col.value.str.split(".").list.first(),
        )
        .unique()
        .group_by(pl.col.tag)
        .agg(pl.col.value)
        .collect(streaming=True)
        .to_dicts()
    )

    case_contexts = list_case_contexts(role)
    contexts_mapping = (
        pl.DataFrame(case_contexts)
        .lazy()
        .select(
            pl.col.tag,
            pl.col.value.str.split(".").list.first(),
        )
        .unique()
        .group_by(pl.col.tag)
        .agg(pl.col.value)
        .collect(streaming=True)
        .to_dicts()
    )
    context_cons = [
        CategoryConstraint.model_validate(d, strict=True) for d in contexts_mapping
    ]
    action_cons = [
        CategoryConstraint.model_validate(d, strict=True) for d in actions_mapping
    ]

    return StreamingResponse(
        stream_case_completions(
            cases, context_cons=context_cons, action_cons=action_cons
        ),
        media_type="text/event-stream",
    )


### Users


@app.put("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    role: Annotated[Role, Depends(authenticate_user)],
) -> User:
    """Create new user.

    Note that this is just for user config, auth is done separately."""

    # Check if user exists

    with Session(engine) as session:
        # Check if user exists
        statement = select(User).where(User.id == role.user_id).limit(1)
        result = session.exec(statement)

        user = result.one_or_none()
        if user is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="User already exists"
            )
        user = User(id=role.user_id)

        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@app.get("/users")
def get_user(
    role: Annotated[Role, Depends(authenticate_user)],
) -> User:
    """Return user as title, description, list of Action JSONs, adjacency list of Action IDs."""

    with Session(engine) as session:
        # Get user given user_id
        statement = select(User).where(User.id == role.user_id)
        result = session.exec(statement)
        try:
            user = result.one()
            return user
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            ) from e


@app.post("/users", status_code=status.HTTP_204_NO_CONTENT)
def update_user(
    role: Annotated[Role, Depends(authenticate_user)],
    params: UpdateUserParams,
) -> None:
    """Update user."""

    with Session(engine) as session:
        statement = select(User).where(User.id == role.user_id)
        result = session.exec(statement)
        try:
            user = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            ) from e

        if params.tier is not None:
            user.tier = params.tier
        if params.settings is not None:
            user.settings = params.settings

        session.add(user)
        session.commit()


@app.delete("/users", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    role: Annotated[Role, Depends(authenticate_user)],
) -> None:
    """Delete user."""

    with Session(engine) as session:
        statement = select(User).where(User.id == role.user_id)
        result = session.exec(statement)
        try:
            user = result.one()
        except NoResultFound as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            ) from e
        session.delete(user)
        session.commit()


### Secrets


@app.get("/secrets")
def list_secrets(
    role: Annotated[Role, Depends()],
) -> list[Secret]:
    """Get a secret by ID."""
    with Session(engine) as session:
        statement = select(Secret).where(Secret.owner_id == role.user_id)
        result = session.exec(statement)
        secrets = result.all()
        return secrets


@app.get("/secrets/{secret_name}")
def get_secret(
    role: Annotated[Role, Depends(authenticate_user_or_service)],
    secret_name: str,
) -> Secret:
    """Get a secret by ID.

    Support access for both user and service roles."""

    logger.info(f"Role: {role}")
    with Session(engine) as session:
        # Check if secret exists
        statement = (
            select(Secret)
            .where(Secret.owner_id == role.user_id, Secret.name == secret_name)
            .limit(1)
        )
        result = session.exec(statement)
        secret = result.one_or_none()
        if secret is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Secret not found"
            )
        return secret


@app.put("/secrets", status_code=status.HTTP_201_CREATED)
def create_secret(
    role: Annotated[Role, Depends(authenticate_user)],
    params: CreateSecretParams,
) -> None:
    """Get a secret by ID."""
    with Session(engine) as session:
        # Check if secret exists
        statement = (
            select(Secret)
            .where(Secret.owner_id == role.user_id, Secret.name == params.name)
            .limit(1)
        )
        result = session.exec(statement)
        secret = result.one_or_none()
        if secret is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Secret already exists"
            )
        new_secret = Secret(owner_id=role.user_id, name=params.name)
        new_secret.key = params.value  # Set and encrypt the key

        session.add(new_secret)
        session.commit()
        session.refresh(new_secret)


@app.post("/secrets", status_code=status.HTTP_201_CREATED)
def update_secret(
    role: Annotated[Role, Depends(authenticate_user)],
    params: UpdateSecretParams,
) -> Secret:
    """Get a secret by ID."""
    with Session(engine) as session:
        # Check if secret exists
        statement = (
            select(Secret)
            .where(Secret.owner_id == role.user_id, Secret.name == params.name)
            .limit(1)
        )
        result = session.exec(statement)
        secret = result.one_or_none()
        if secret is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Secret does not exist"
            )
        secret.key = params.value  # Set and encrypt the key
        session.add(secret)
        session.commit()
        session.refresh(secret)


@app.post("/secrets/search")
def search_secrets(
    role: Annotated[Role, Depends(authenticate_user)],
    params: SearchSecretsParams,
) -> list[Secret]:
    """Get a secret by ID."""
    with Session(engine) as session:
        statement = (
            select(Secret)
            .where(Secret.owner_id == role.user_id)
            .filter(*[Secret.name == name for name in params.names])
        )
        result = session.exec(statement)
        secrets = result.all()
        return secrets
