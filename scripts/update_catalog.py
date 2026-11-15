"""Refresh the reviewed API snapshot from Hetzner's official OpenAPI document."""
import datetime
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

URL = "https://docs.hetzner.cloud/cloud.spec.json"
ROOT = Path(__file__).resolve().parents[1]


def main():
    with urlopen(URL, timeout=90) as response:
        raw = response.read()
    spec = json.loads(raw)
    operations = []
    for path, methods in sorted(spec["paths"].items()):
        for method, operation in sorted(methods.items()):
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operations.append({
                "id": operation["operationId"], "method": method.upper(), "path": path,
                "summary": operation.get("summary", operation["operationId"]),
                "tag": operation.get("tags", ["Other"])[0],
                "parameters": methods.get("parameters", []) + operation.get("parameters", []),
                "requestBody": operation.get("requestBody", {}),
                "deprecated": operation.get("deprecated", False),
            })
    result = {
        "source": URL, "sha256": hashlib.sha256(raw).hexdigest(),
        "retrieved": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "components": spec.get("components", {}), "operations": operations,
    }
    (ROOT / "bot" / "catalog.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Saved {len(operations)} operations from {URL}")


if __name__ == "__main__":
    main()
