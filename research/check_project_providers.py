"""Only read model metadata from endpoints explicitly configured by the user."""
from pathlib import Path
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]


def config():
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = json.loads(value) if value.startswith('"') else value
    return values


def inspect():
    values = config()
    checks = [
        ("dashscope", "DASHSCOPE_API_KEY", "DASHSCOPE_TEXT_BASE_URL", [values["CONTEXT_TEXT_MODEL"], values["LIVE_TRANSLATION_MODEL"]]),
        ("aihubmix", "AIHUBMIX_API_KEY", "AIHUBMIX_BASE_URL", [values["AIHUBMIX_TEXT_MODEL"]]),
    ]
    rows = []
    for name, key_field, url_field, models in checks:
        base = values[url_field].rstrip("/")
        host = urlsplit(base).hostname
        if urlsplit(base).scheme != "https" or host not in {"dashscope.aliyuncs.com", "aihubmix.com"}:
            raise ValueError("Unexpected endpoint in current user configuration")
        row = {"provider": name, "endpoint_host": host, "requested_models": models}
        request = Request(base + "/models", headers={"Authorization": "Bearer " + values[key_field]})
        try:
            with urlopen(request, timeout=20) as response:
                data = json.load(response)
            available = {m.get("id", "") for m in data.get("data", [])}
            row.update(http_status=200, listed_model_count=len(available),
                       requested_models_listed={model: model in available for model in models})
        except HTTPError as error:
            row["http_status"] = error.code
        except (URLError, TimeoutError, OSError, ValueError) as error:
            row.update(http_status="unavailable", error_type=type(error).__name__)
        rows.append(row)
    result = {"checked_at": datetime.now(timezone.utc).isoformat(), "paid_generation_calls": 0, "providers": rows}
    (ROOT / "research" / "project_provider_health.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    inspect()
