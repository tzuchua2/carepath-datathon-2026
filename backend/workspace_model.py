"""Chat adapter for the Databricks-hosted verification job. Uses the workspace identity."""
import re
import threading

from backend.verifier import ModelUnavailable, OUTPUT_SCHEMA


DEFAULT_ENDPOINT = "databricks-gpt-oss-20b"


def workspace_model(endpoint=DEFAULT_ENDPOINT):
    import requests
    from databricks.sdk import WorkspaceClient

    if not re.fullmatch(r"databricks-[a-z0-9-]+", endpoint):
        raise ValueError("Choose a Databricks foundation model endpoint name")
    config = WorkspaceClient().config
    url = f"{config.host.rstrip('/')}/serving-endpoints/{endpoint}/invocations"
    throttled = threading.Event()

    def call(system, user):
        if throttled.is_set():
            raise ModelUnavailable("Workspace quota reached earlier in this run")
        response = requests.post(url, headers=config.authenticate(), json={
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0, "max_tokens": 6000,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "service_findings", "schema": OUTPUT_SCHEMA, "strict": True,
            }},
        }, timeout=90, allow_redirects=False)
        if response.status_code == 429:
            throttled.set()
        if response.status_code != 200:
            raise ModelUnavailable(f"Databricks inference returned HTTP {response.status_code}")
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ModelUnavailable("Databricks returned an incomplete model reply")
        return choice["message"]["content"], endpoint

    call.model_name = endpoint
    return call
