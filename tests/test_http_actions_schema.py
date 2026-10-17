"""Current HTTP router and DTO schemas, with no application/store/process composition."""

import pytest
from fastapi import FastAPI

from terminal_mcp.http.actions import build_actions_router
from terminal_mcp.http.console import build_console_router
from terminal_mcp.version import __version__


@pytest.fixture(scope="module")
def schema():
    app = FastAPI(version=__version__)
    app.include_router(build_actions_router(None))
    app.include_router(build_console_router(None, None))
    return app.openapi()


def test_action_router_run_operation_id(schema):
    assert schema["paths"]["/actions/run"]["post"]["operationId"] == "runCommand"


def test_task_list_route_binds_the_canonical_request_schema(schema):
    operation = schema["paths"]["/actions/tasks"]["post"]
    ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/TasksRequest"
    assert "/actions/task" not in schema["paths"]
