"""Only catalogued paths may reach the fixed Hetzner Cloud HTTPS origin."""
import json
import re
from pathlib import Path
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator

CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
OPERATIONS = CATALOG["operations"]
BY_ID = {op["id"]: op for op in OPERATIONS}


def schema_for(op):
    properties = {"path": {"type": "object", "properties": {}, "additionalProperties": False},
                  "query": {"type": "object", "properties": {}, "additionalProperties": False}}
    required = []
    for parameter in op["parameters"]:
        location = parameter["in"]
        if location not in properties:
            continue
        group = properties[location]
        group["properties"][parameter["name"]] = parameter["schema"]
        if parameter.get("required"):
            group.setdefault("required", []).append(parameter["name"])
            if location not in required:
                required.append(location)
    request = op["requestBody"]
    if request:
        properties["body"] = request["content"]["application/json"]["schema"]
        if request.get("required"):
            required.append("body")
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False, "components": CATALOG["components"]}


def sample(schema):
    if "$ref" in schema:
        value = CATALOG
        for part in schema["$ref"].removeprefix("#/").split("/"):
            value = value[part]
        return sample(value)
    if "example" in schema:
        return schema["example"]
    if "examples" in schema and isinstance(schema["examples"], list) and schema["examples"]:
        return schema["examples"][0]
    if "enum" in schema:
        return schema["enum"][0]
    for union in ("oneOf", "anyOf", "allOf"):
        if union in schema:
            return sample(schema[union][0])
    kind = schema.get("type", "object")
    if isinstance(kind, list):
        kind = next((x for x in kind if x != "null"), "null")
    if kind == "object":
        return {k: sample(v) for k, v in schema.get("properties", {}).items()
                if k in schema.get("required", [])}
    if kind == "array":
        return [sample(schema.get("items", {}))]
    return {"integer": max(schema.get("minimum", 1), 1), "number": 1,
            "boolean": False, "null": None}.get(kind, "REPLACE_ME")


def prepare(op, payload):
    errors = sorted(Draft202012Validator(schema_for(op)).iter_errors(payload),
                    key=lambda e: str(list(e.path)))
    if errors:
        # Do not echo user values: they may contain credentials or private keys.
        field = ".".join(str(p) for p in errors[0].path) or "root"
        raise ValueError(f"ورودی نامعتبر در {field}؛ قانون: {errors[0].validator}")
    path = op["path"]
    for name, value in payload.get("path", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(value)):
            raise ValueError("شناسهٔ مسیر نامعتبر است.")
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    if "{" in path:
        raise ValueError("شناسهٔ منبع وارد نشده است.")
    query = []
    for name, value in payload.get("query", {}).items():
        for item in value if isinstance(value, list) else [value]:
            query.append((name, str(item).lower() if isinstance(item, bool) else str(item)))
    return path, query, payload.get("body")


class CloudError(Exception):
    pass


class Cloud:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def execute(self, token, op, payload):
        path, query, body = prepare(op, payload)
        try:
            response = await self.client.request(
                op["method"], "https://api.hetzner.cloud/v1" + path,
                headers={"Authorization": "Bearer " + token}, params=query, json=body,
            )
        except httpx.HTTPError:
            raise CloudError("خطای ارتباط؛ نتیجه نامشخص است. پیش از تکرار، وضعیت منبع را بررسی کنید.") from None
        if response.is_error:
            # Server error messages may echo request secrets; expose only the stable code.
            try:
                code = response.json().get("error", {}).get("code", "unknown")
            except (ValueError, AttributeError):
                code = "unknown"
            code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code) else "unknown"
            wait = " محدودیت درخواست؛ کمی صبر کنید." if response.status_code == 429 else ""
            raise CloudError(f"Hetzner HTTP {response.status_code}: {code}.{wait}")
        return response.json() if response.content else {"status": "accepted"}


SECRET_WORDS = ("password", "private_key", "token", "secret", "user_data", "certificate", "ticket", "wss_url")


def redact(value, secrets=(), hide_fields=True):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if hide_fields and any(s in k.lower() for s in SECRET_WORDS)
                else redact(v, secrets, hide_fields) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets, hide_fields) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value
