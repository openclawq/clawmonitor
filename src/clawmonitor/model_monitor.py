from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib import error as urlerror
from urllib import request as urlrequest
from uuid import uuid4

from .openclaw_cli import gateway_call
from .openclaw_config import read_openclaw_config_snapshot
from .redact import redact_text
from .session_store import list_sessions
from .session_tail import tail_for_meta


DEFAULT_MODEL_PROBE_PROMPT = "Reply with exactly OK."
_ENV_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)\s*$")
_ENV_FILE_CACHE: Dict[Path, Dict[str, str]] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get(d: Any, *path: str) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _clean_text(text: Any, *, limit: int = 240) -> str:
    if text is None:
        return ""
    s = redact_text(str(text))
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "..."


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _fit(text: str, width: int) -> str:
    s = _clean_text(text, limit=max(1, width))
    if len(s) <= width:
        return s.ljust(width)
    if width <= 1:
        return s[:width]
    return s[: width - 1] + "."


def _age_seconds(dt: Optional[datetime]) -> Optional[int]:
    if not dt:
        return None
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((_utc_now() - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def _fmt_age(dt: Optional[datetime]) -> str:
    age = _age_seconds(dt)
    if age is None:
        return "-"
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    return f"{age // 3600}h"


def _status_rank(status: str) -> int:
    order = {
        "ok": 0,
        "degraded": 1,
        "timeout": 2,
        "rate_limit": 3,
        "overloaded": 4,
        "auth": 5,
        "billing": 6,
        "network": 7,
        "unsupported": 8,
        "error": 9,
        "unknown": 10,
    }
    return order.get((status or "").strip(), 99)


def _display_status(status: str) -> str:
    mapping = {
        "ok": "OK",
        "degraded": "DEGRADED",
        "timeout": "TIMEOUT",
        "rate_limit": "RATE",
        "overloaded": "OVERLOADED",
        "auth": "AUTH",
        "billing": "BILLING",
        "network": "NETWORK",
        "unsupported": "UNSUPPORTED",
        "error": "ERROR",
        "unknown": "UNKNOWN",
    }
    return mapping.get(status, (status or "-").upper())


def _connection_state(status: str) -> str:
    if status == "ok":
        return "healthy"
    if status in ("timeout", "rate_limit", "overloaded", "degraded"):
        return "degraded"
    if status in ("unsupported", "unknown"):
        return "unknown"
    return "down"


@dataclass(frozen=True)
class ProbeResult:
    method: str
    status: str
    connection: str
    checked_at: Optional[datetime]
    latency_ms: Optional[int]
    efficiency: Optional[float]
    efficiency_unit: Optional[str]
    detail: str
    reply_preview: str
    http_status: Optional[int]
    ok: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "status": self.status,
            "connection": self.connection,
            "checkedAt": _iso(self.checked_at),
            "latencyMs": self.latency_ms,
            "efficiency": self.efficiency,
            "efficiencyUnit": self.efficiency_unit,
            "detail": self.detail,
            "replyPreview": self.reply_preview,
            "httpStatus": self.http_status,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class ModelTarget:
    agent_id: str
    agent_label: str
    agent_dir: Path
    model_ref: str
    provider_id: str
    model_id: str
    model_label: str
    roles: Tuple[str, ...]
    api_kind: str
    base_url: Optional[str]
    auth_source: Optional[str]
    headers: Dict[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agentId": self.agent_id,
            "agentLabel": self.agent_label,
            "agentDir": str(self.agent_dir),
            "modelRef": self.model_ref,
            "providerId": self.provider_id,
            "modelId": self.model_id,
            "modelLabel": self.model_label,
            "roles": list(self.roles),
            "apiKind": self.api_kind,
            "baseUrl": self.base_url,
            "authSource": self.auth_source,
            "headers": dict(self.headers),
        }


@dataclass(frozen=True)
class ModelRow:
    target: ModelTarget
    direct: Optional[ProbeResult]
    openclaw: Optional[ProbeResult]
    overall_status: str
    overall_connection: str
    summary: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agentId": self.target.agent_id,
            "agentLabel": self.target.agent_label,
            "modelRef": self.target.model_ref,
            "providerId": self.target.provider_id,
            "modelId": self.target.model_id,
            "modelLabel": self.target.model_label,
            "roles": list(self.target.roles),
            "apiKind": self.target.api_kind,
            "baseUrl": self.target.base_url,
            "authSource": self.target.auth_source,
            "overallStatus": self.overall_status,
            "overallConnection": self.overall_connection,
            "summary": self.summary,
            "direct": self.direct.to_dict() if self.direct else None,
            "openclaw": self.openclaw.to_dict() if self.openclaw else None,
        }


@dataclass(frozen=True)
class ModelProbeOptions:
    prompt: str = DEFAULT_MODEL_PROBE_PROMPT
    timeout_seconds: int = 20
    include_direct: bool = True
    include_openclaw: bool = True
    max_workers: int = 4


def _extract_model_chain(model_cfg: Any) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    if isinstance(model_cfg, str) and model_cfg.strip():
        return [("primary", model_cfg.strip())]
    if isinstance(model_cfg, list):
        for idx, item in enumerate(model_cfg):
            if not isinstance(item, str) or not item.strip():
                continue
            out.append(("primary" if idx == 0 else f"fallback{idx}", item.strip()))
        return out
    if not isinstance(model_cfg, dict):
        return out

    primary = model_cfg.get("primary")
    if isinstance(primary, str) and primary.strip():
        out.append(("primary", primary.strip()))

    fallbacks = model_cfg.get("fallbacks")
    if isinstance(fallbacks, list):
        for idx, item in enumerate(fallbacks, start=1):
            if not isinstance(item, str) or not item.strip():
                continue
            out.append((f"fallback{idx}", item.strip()))

    secondary = model_cfg.get("secondary")
    if isinstance(secondary, str) and secondary.strip():
        out.append(("secondary", secondary.strip()))
    return out


def _normalize_headers(headers: Any, *, agent_dir: Optional[Path] = None) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(headers, dict):
        return out
    for key, value in headers.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(value, str):
            continue
        resolved = _resolve_secret(value, agent_dir=agent_dir)[0] or value
        out[key.strip()] = resolved
    return out


def _find_openclaw_root(agent_dir: Optional[Path]) -> Optional[Path]:
    if agent_dir is None:
        return None
    start = agent_dir.expanduser().resolve()
    for candidate in (start, *start.parents):
        if (candidate / "openclaw.json").exists():
            return candidate
    return None


def _strip_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _load_openclaw_env(root: Optional[Path]) -> Dict[str, str]:
    if root is None:
        return {}
    env_path = root / ".env"
    cached = _ENV_FILE_CACHE.get(env_path)
    if cached is not None:
        return cached
    parsed: Dict[str, str] = {}
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = _ENV_ASSIGN_RE.match(line)
            if not match:
                continue
            parsed[match.group(1)] = _strip_env_value(match.group(2))
    except Exception:
        parsed = {}
    _ENV_FILE_CACHE[env_path] = parsed
    return parsed


def _resolve_env_value(env_name: str, *, agent_dir: Optional[Path] = None) -> Optional[str]:
    value = os.environ.get(env_name)
    if value:
        return value
    root = _find_openclaw_root(agent_dir)
    if root is None:
        return None
    return _load_openclaw_env(root).get(env_name)


def _resolve_secret(value: Any, *, agent_dir: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(value, str):
        return None, None
    raw = value.strip()
    if not raw:
        return None, None
    lowered = raw.lower()
    braced_env = re.fullmatch(r"\$\{([A-Z][A-Z0-9_]*)\}", raw)
    if braced_env:
        env_name = braced_env.group(1)
        return _resolve_env_value(env_name, agent_dir=agent_dir), f"env:{env_name}"
    if lowered.startswith("secretref-env:"):
        env_name = raw.split(":", 1)[1].strip()
        return _resolve_env_value(env_name, agent_dir=agent_dir), f"env:{env_name}"
    if lowered.startswith("env:"):
        env_name = raw.split(":", 1)[1].strip()
        return _resolve_env_value(env_name, agent_dir=agent_dir), f"env:{env_name}"
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", raw):
        return _resolve_env_value(raw, agent_dir=agent_dir), f"env:{raw}"
    return raw, "literal"


def _resolve_auth_value(provider_id: str, provider_conf: Dict[str, Any], agent_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    auth_path = agent_dir / "auth-profiles.json"
    auth_doc = _safe_load_json(auth_path)
    profiles = auth_doc.get("profiles")
    last_good = auth_doc.get("lastGood")

    selected_profile: Optional[Dict[str, Any]] = None
    selected_profile_name: Optional[str] = None

    if isinstance(profiles, dict):
        profile_id = None
        if isinstance(last_good, dict):
            raw = last_good.get(provider_id)
            if isinstance(raw, str):
                profile_id = raw
            elif isinstance(raw, dict):
                cand = raw.get("profile")
                if isinstance(cand, str):
                    profile_id = cand
        if profile_id and isinstance(profiles.get(profile_id), dict):
            selected_profile_name = profile_id
            selected_profile = profiles.get(profile_id)
        else:
            for key, prof in profiles.items():
                if not isinstance(prof, dict):
                    continue
                if str(prof.get("provider") or "").strip() == provider_id:
                    selected_profile_name = str(key)
                    selected_profile = prof
                    break

    if isinstance(selected_profile, dict):
        for auth_key in ("key", "token", "apiKey", "accessToken", "value"):
            resolved, source = _resolve_secret(selected_profile.get(auth_key), agent_dir=agent_dir)
            if resolved:
                src = f"profile:{selected_profile_name}" if selected_profile_name else "profile"
                if source and source != "literal":
                    src = f"{src}/{source}"
                return resolved, src

    for auth_key in ("apiKey", "token", "key"):
        resolved, source = _resolve_secret(provider_conf.get(auth_key), agent_dir=agent_dir)
        if resolved:
            src = f"provider:{provider_id}"
            if source and source != "literal":
                src = f"{src}/{source}"
            return resolved, src

    return None, None


def _detect_api_kind(provider_conf: Dict[str, Any], *, base_url: Optional[str]) -> str:
    api = str(provider_conf.get("api") or provider_conf.get("transport") or provider_conf.get("kind") or "").strip().lower()
    if api in ("openai-completions", "openai-responses", "anthropic-messages"):
        return api

    headers = provider_conf.get("headers")
    if isinstance(headers, dict):
        lowered = {str(k).lower(): str(v).lower() for k, v in headers.items() if isinstance(k, str) and isinstance(v, str)}
        if "anthropic-version" in lowered:
            return "anthropic-messages"

    base = (base_url or "").lower()
    if "anthropic" in base:
        return "anthropic-messages"
    if base.endswith("/responses") or "/openai" in base:
        return "openai-responses"
    if base:
        return "openai-completions"
    return "unknown"


def _provider_base_url(provider_conf: Dict[str, Any]) -> Optional[str]:
    for key in ("baseUrl", "apiBase", "url", "endpoint"):
        value = provider_conf.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _join_endpoint(base_url: str, suffix: str) -> str:
    base = (base_url or "").rstrip("/")
    path = suffix.lstrip("/")
    if not base:
        return path
    if base.lower().endswith(path.lower()):
        return base
    return f"{base}/{path}"


def _extract_model_label(model_ref: str, provider_conf: Dict[str, Any], alias_map: Dict[str, str]) -> str:
    alias = alias_map.get(model_ref)
    if alias:
        return alias
    model_id = model_ref.split("/", 1)[1] if "/" in model_ref else model_ref
    models = provider_conf.get("models")
    if isinstance(models, list):
        for item in models:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() != model_id:
                continue
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return model_id


def _extract_model_api_kind(model_ref: str, provider_conf: Dict[str, Any]) -> Optional[str]:
    model_id = model_ref.split("/", 1)[1] if "/" in model_ref else model_ref
    models = provider_conf.get("models")
    if not isinstance(models, list):
        return None
    for item in models:
        if not isinstance(item, dict):
            continue
        if str(item.get("id") or "").strip() != model_id:
            continue
        api = str(item.get("api") or "").strip().lower()
        if api in ("openai-completions", "openai-responses", "anthropic-messages"):
            return api
        return None
    return None


def discover_model_targets(openclaw_root: Path) -> List[ModelTarget]:
    doc = _safe_load_json(openclaw_root / "openclaw.json")
    providers = _get(doc, "models", "providers")
    if not isinstance(providers, dict):
        providers = doc.get("providers") if isinstance(doc.get("providers"), dict) else {}

    cfg_snapshot = read_openclaw_config_snapshot(openclaw_root)
    agents_doc = doc.get("agents") if isinstance(doc, dict) else {}
    defaults = agents_doc.get("defaults") if isinstance(agents_doc, dict) else {}
    default_model_cfg = defaults.get("model") if isinstance(defaults, dict) else None
    default_workspace = defaults.get("workspace") if isinstance(defaults, dict) else None
    default_agent_dir = openclaw_root / "agents" / "main" / "agent"
    if isinstance(default_workspace, str) and default_workspace.strip():
        default_agent_dir = openclaw_root / "agents" / "main" / "agent"

    alias_map: Dict[str, str] = {}
    model_aliases = defaults.get("models") if isinstance(defaults, dict) else None
    if isinstance(model_aliases, dict):
        for model_ref, model_meta in model_aliases.items():
            if not isinstance(model_ref, str):
                continue
            if isinstance(model_meta, dict):
                alias = model_meta.get("alias") or model_meta.get("name")
                if isinstance(alias, str) and alias.strip():
                    alias_map[model_ref] = alias.strip()

    agents_list = agents_doc.get("list") if isinstance(agents_doc, dict) else None
    normalized_agents: List[Dict[str, Any]] = []
    if isinstance(agents_list, list) and agents_list:
        for item in agents_list:
            if not isinstance(item, dict):
                continue
            agent_id = item.get("id")
            if not isinstance(agent_id, str) or not agent_id.strip():
                continue
            normalized_agents.append(item)
    else:
        normalized_agents.append({"id": "main"})

    targets: List[ModelTarget] = []
    for ent in normalized_agents:
        agent_id = str(ent.get("id") or "").strip()
        if not agent_id:
            continue
        agent_dir = ent.get("agentDir")
        if isinstance(agent_dir, str) and agent_dir.strip():
            agent_dir_path = Path(agent_dir).expanduser()
        else:
            agent_dir_path = openclaw_root / "agents" / agent_id / "agent"
        model_cfg = ent.get("model")
        if model_cfg is None:
            model_cfg = default_model_cfg
        chain = _extract_model_chain(model_cfg)
        merged: Dict[str, List[str]] = {}
        for role, model_ref in chain:
            merged.setdefault(model_ref, [])
            merged[model_ref].append(role)
        for model_ref, roles in merged.items():
            provider_id, model_id = _split_model_ref(model_ref)
            provider_conf = providers.get(provider_id) if isinstance(providers, dict) else None
            if not isinstance(provider_conf, dict):
                provider_conf = {}
            base_url = _provider_base_url(provider_conf)
            api_kind = _extract_model_api_kind(model_ref, provider_conf) or _detect_api_kind(
                provider_conf, base_url=base_url
            )
            auth_value, auth_source = _resolve_auth_value(provider_id, provider_conf, agent_dir_path)
            headers = _normalize_headers(provider_conf.get("headers"), agent_dir=agent_dir_path)
            if auth_value:
                headers = _with_auth_headers(headers, provider_conf, api_kind=api_kind, auth_value=auth_value)
            targets.append(
                ModelTarget(
                    agent_id=agent_id,
                    agent_label=cfg_snapshot.agent_label(agent_id),
                    agent_dir=agent_dir_path,
                    model_ref=model_ref,
                    provider_id=provider_id,
                    model_id=model_id,
                    model_label=_extract_model_label(model_ref, provider_conf, alias_map),
                    roles=tuple(roles),
                    api_kind=api_kind,
                    base_url=base_url,
                    auth_source=auth_source,
                    headers=headers,
                )
            )

    targets.sort(key=lambda t: (t.agent_id, t.provider_id, t.model_id))
    return targets


def _split_model_ref(model_ref: str) -> Tuple[str, str]:
    raw = (model_ref or "").strip()
    if "/" in raw:
        provider_id, model_id = raw.split("/", 1)
        return provider_id.strip(), model_id.strip()
    return "", raw


def _with_auth_headers(headers: Dict[str, str], provider_conf: Dict[str, Any], *, api_kind: str, auth_value: str) -> Dict[str, str]:
    out = dict(headers)
    auth_header = provider_conf.get("authHeader")
    auth_mode = str(provider_conf.get("auth") or "").strip().lower()
    if api_kind == "anthropic-messages":
        out.setdefault("x-api-key", auth_value)
        out.setdefault("anthropic-version", "2023-06-01")
        return out

    if isinstance(auth_header, str) and auth_header.strip():
        header_name = auth_header.strip()
        if header_name.lower() == "authorization":
            out.setdefault(header_name, f"Bearer {auth_value}")
        else:
            out.setdefault(header_name, auth_value)
        return out

    if auth_header is True:
        out.setdefault("Authorization", f"Bearer {auth_value}")
        return out

    if auth_mode in ("x-api-key", "api-key-header"):
        out.setdefault("x-api-key", auth_value)
        return out

    out.setdefault("Authorization", f"Bearer {auth_value}")
    return out


def _http_json(
    url: str,
    *,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout_seconds: int,
) -> Tuple[int, Any, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = dict(headers)
    req_headers.setdefault("Content-Type", "application/json")
    req_headers.setdefault("Accept", "application/json")
    req = urlrequest.Request(url, data=body, headers=req_headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                if "text/event-stream" in str(resp.headers.get("Content-Type", "")).lower() or raw.lstrip().startswith("data:"):
                    doc = _parse_sse_payload(raw)
                else:
                    doc = json.loads(raw) if raw.strip() else {}
            except Exception:
                doc = {}
            return int(getattr(resp, "status", 200) or 200), doc, raw
    except urlerror.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            doc = json.loads(raw) if raw.strip() else {}
        except Exception:
            doc = {}
        return int(getattr(exc, "code", 0) or 0), doc, raw


def _parse_sse_payload(raw: str) -> Dict[str, Any]:
    events: List[Dict[str, Any]] = []
    deltas: List[str] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text.startswith("data:"):
            continue
        payload = text[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except Exception:
            continue
        if isinstance(event, dict):
            events.append(event)
            if isinstance(event.get("delta"), str):
                deltas.append(str(event.get("delta")))
    for event in reversed(events):
        response = event.get("response")
        if isinstance(response, dict):
            if deltas and not response.get("output_text"):
                response = dict(response)
                response["output_text"] = "".join(deltas)
            return response
    if deltas:
        return {"output_text": "".join(deltas)}
    return {}


def _extract_reply_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "value"):
                    if isinstance(item.get(key), str):
                        parts.append(item.get(key))
                        break
                else:
                    inner = item.get("content")
                    txt = _extract_reply_text(inner)
                    if txt:
                        parts.append(txt)
        return " ".join([p for p in parts if p]).strip()
    if isinstance(value, dict):
        for key in ("text", "value", "output_text"):
            if isinstance(value.get(key), str):
                return str(value.get(key))
        return _extract_reply_text(value.get("content"))
    return ""


def _extract_usage_info(api_kind: str, doc: Any) -> Tuple[str, Optional[float], Optional[str]]:
    if not isinstance(doc, dict):
        return "", None, None

    reply = ""
    usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
    output_units: Optional[float] = None

    if api_kind == "openai-completions":
        choices = doc.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                msg = first.get("message")
                if isinstance(msg, dict):
                    reply = _extract_reply_text(msg.get("content"))
        output_units = _safe_float(usage.get("completion_tokens"))

    elif api_kind == "openai-responses":
        reply = _extract_reply_text(doc.get("output_text"))
        if not reply:
            output = doc.get("output")
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    reply = _extract_reply_text(item.get("content"))
                    if reply:
                        break
        output_units = _safe_float(usage.get("output_tokens"))

    elif api_kind == "anthropic-messages":
        reply = _extract_reply_text(doc.get("content"))
        output_units = _safe_float(usage.get("output_tokens"))

    return reply.strip(), output_units, "tok/s" if output_units is not None else None


def _classify_error(detail: str, *, http_status: Optional[int] = None, network_error: bool = False) -> str:
    text = (detail or "").lower()
    if network_error:
        return "network"
    if http_status in (408, 504) or "timed out" in text or "timeout" in text:
        return "timeout"
    if http_status in (401, 403):
        return "auth"
    if http_status == 402:
        return "billing"
    if http_status == 429:
        if any(token in text for token in ("quota", "balance", "credit", "insufficient", "payment", "billing", "recharge", "subscription", "套餐", "权限", "额度", "余额")):
            return "billing"
        return "rate_limit"
    if http_status in (502, 503):
        return "overloaded"
    if http_status in (404, 405, 410, 501):
        return "unsupported"
    if any(token in text for token in ("unauthorized", "forbidden", "invalid api key", "authentication", "auth failed")):
        return "auth"
    if any(token in text for token in ("insufficient_quota", "insufficient balance", "out of credit", "billing", "subscription", "套餐", "权限", "额度", "余额不足")):
        return "billing"
    if any(token in text for token in ("rate limit", "too many requests", "too many tokens", "requests limit", "频率", "限流", "请求过多")):
        return "rate_limit"
    if any(token in text for token in ("overloaded", "capacity", "temporarily unavailable", "service unavailable", "bad gateway")):
        return "overloaded"
    if any(token in text for token in ("network", "name or service not known", "temporary failure in name resolution", "connection refused", "ssl", "connection reset")):
        return "network"
    if any(token in text for token in ("unsupported", "not supported", "unknown model", "model not found", "not found")):
        return "unsupported"
    if any(token in text for token in ("timeout", "timed out")):
        return "timeout"
    return "error"


def _probe_result(
    *,
    method: str,
    status: str,
    checked_at: Optional[datetime],
    latency_ms: Optional[int],
    efficiency: Optional[float],
    efficiency_unit: Optional[str],
    detail: str,
    reply_preview: str,
    http_status: Optional[int],
) -> ProbeResult:
    return ProbeResult(
        method=method,
        status=status,
        connection=_connection_state(status),
        checked_at=checked_at,
        latency_ms=latency_ms,
        efficiency=efficiency,
        efficiency_unit=efficiency_unit,
        detail=_clean_text(detail, limit=240),
        reply_preview=_clean_text(reply_preview, limit=160),
        http_status=http_status,
        ok=(status == "ok"),
    )


def probe_direct(target: ModelTarget, *, prompt: str, timeout_seconds: int) -> ProbeResult:
    started_at = _utc_now()
    if not target.base_url:
        return _probe_result(
            method="direct",
            status="unsupported",
            checked_at=started_at,
            latency_ms=None,
            efficiency=None,
            efficiency_unit=None,
            detail="Provider config is missing baseUrl/apiBase/url.",
            reply_preview="",
            http_status=None,
        )
    if target.api_kind not in ("openai-completions", "openai-responses", "anthropic-messages"):
        return _probe_result(
            method="direct",
            status="unsupported",
            checked_at=started_at,
            latency_ms=None,
            efficiency=None,
            efficiency_unit=None,
            detail=f"Unsupported provider api kind: {target.api_kind or 'unknown'}.",
            reply_preview="",
            http_status=None,
        )

    if target.api_kind == "openai-completions":
        url = _join_endpoint(target.base_url, "chat/completions")
        payload = {
            "model": target.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 16,
        }
    elif target.api_kind == "openai-responses":
        url = _join_endpoint(target.base_url, "responses")
        payload = {
            "model": target.model_id,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            "max_output_tokens": 16,
            "stream": True,
        }
    else:
        url = _join_endpoint(target.base_url, "messages")
        payload = {
            "model": target.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
        }

    t0 = time.monotonic()
    try:
        http_status, doc, raw = _http_json(url, headers=target.headers, payload=payload, timeout_seconds=timeout_seconds)
        latency_ms = int((time.monotonic() - t0) * 1000)
        reply_text, output_units, unit = _extract_usage_info(target.api_kind, doc)
        if 200 <= http_status < 300:
            efficiency = None
            eff_unit = None
            if output_units is not None and latency_ms > 0:
                efficiency = round(output_units / max(latency_ms / 1000.0, 0.001), 3)
                eff_unit = unit
            elif reply_text and latency_ms > 0:
                efficiency = round(len(reply_text) / max(latency_ms / 1000.0, 0.001), 3)
                eff_unit = "char/s"
            detail = "Probe succeeded."
            if not reply_text:
                detail = "Probe succeeded but provider returned no textual reply preview."
            return _probe_result(
                method="direct",
                status="ok",
                checked_at=_utc_now(),
                latency_ms=latency_ms,
                efficiency=efficiency,
                efficiency_unit=eff_unit,
                detail=detail,
                reply_preview=reply_text,
                http_status=http_status,
            )

        detail = _extract_error_text(doc, raw)
        status = _classify_error(detail, http_status=http_status)
        return _probe_result(
            method="direct",
            status=status,
            checked_at=_utc_now(),
            latency_ms=latency_ms,
            efficiency=None,
            efficiency_unit=None,
            detail=detail or f"HTTP {http_status}",
            reply_preview="",
            http_status=http_status,
        )
    except (socket.timeout, TimeoutError) as exc:
        return _probe_result(
            method="direct",
            status="timeout",
            checked_at=_utc_now(),
            latency_ms=int((time.monotonic() - t0) * 1000),
            efficiency=None,
            efficiency_unit=None,
            detail=str(exc) or "Direct probe timed out.",
            reply_preview="",
            http_status=None,
        )
    except urlerror.URLError as exc:
        reason = getattr(exc, "reason", exc)
        detail = str(reason)
        status = _classify_error(detail, network_error=True)
        return _probe_result(
            method="direct",
            status=status,
            checked_at=_utc_now(),
            latency_ms=int((time.monotonic() - t0) * 1000),
            efficiency=None,
            efficiency_unit=None,
            detail=detail,
            reply_preview="",
            http_status=None,
        )
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        status = _classify_error(detail)
        return _probe_result(
            method="direct",
            status=status,
            checked_at=_utc_now(),
            latency_ms=int((time.monotonic() - t0) * 1000),
            efficiency=None,
            efficiency_unit=None,
            detail=detail,
            reply_preview="",
            http_status=None,
        )


def _extract_error_text(doc: Any, raw: str) -> str:
    if isinstance(doc, dict):
        for key in ("error", "message", "detail"):
            value = doc.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for inner in ("message", "detail", "type"):
                    inner_val = value.get(inner)
                    if isinstance(inner_val, str) and inner_val.strip():
                        return inner_val.strip()
    return raw.strip()


def probe_openclaw(
    target: ModelTarget,
    *,
    openclaw_root: Path,
    openclaw_bin: str,
    prompt: str,
    timeout_seconds: int,
) -> ProbeResult:
    started_at = _utc_now()
    t0 = time.monotonic()
    session_key = f"agent:{target.agent_id}:modelprobe:{uuid4().hex[:12]}"
    run_key = f"modelprobe-{uuid4().hex[:12]}"
    timeout_ms = max(1000, int(timeout_seconds * 1000))
    agent_timeout = max(5, int(timeout_seconds))

    def fail(detail: str, *, http_status: Optional[int] = None) -> ProbeResult:
        status = _classify_error(detail, http_status=http_status)
        return _probe_result(
            method="openclaw",
            status=status,
            checked_at=_utc_now(),
            latency_ms=int((time.monotonic() - t0) * 1000),
            efficiency=None,
            efficiency_unit=None,
            detail=detail,
            reply_preview="",
            http_status=http_status,
        )

    try:
        patched = gateway_call(
            openclaw_bin,
            "sessions.patch",
            {"key": session_key, "model": target.model_ref},
            timeout_ms=timeout_ms,
        )
        if not patched.ok:
            return fail(_extract_gateway_error(patched) or "sessions.patch failed.")

        dispatched = gateway_call(
            openclaw_bin,
            "agent",
            {
                "sessionKey": session_key,
                "message": prompt,
                "timeout": agent_timeout,
                "deliver": False,
                "idempotencyKey": run_key,
            },
            timeout_ms=timeout_ms,
        )
        if not dispatched.ok:
            return fail(_extract_gateway_error(dispatched) or "agent call failed.")

        wait_doc = gateway_call(
            openclaw_bin,
            "agent.wait",
            {"runId": run_key, "timeoutMs": timeout_ms},
            timeout_ms=timeout_ms + 1000,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if not wait_doc.ok:
            return fail(_extract_gateway_error(wait_doc) or "agent.wait failed.")
        data = wait_doc.data or {}
        status = str(data.get("status") or "").strip().lower()
        if status == "ok":
            reply_preview = ""
            try:
                meta = next((m for m in list_sessions(openclaw_root) if m.key == session_key), None)
                if meta:
                    tail, _ = tail_for_meta(meta, transcript_tail_bytes=32768)
                    if tail.last_assistant and tail.last_assistant.preview:
                        reply_preview = tail.last_assistant.preview
            except Exception:
                reply_preview = ""
            efficiency = None
            eff_unit = None
            if reply_preview and latency_ms > 0:
                efficiency = round(len(reply_preview) / max(latency_ms / 1000.0, 0.001), 3)
                eff_unit = "char/s"
            return _probe_result(
                method="openclaw",
                status="ok",
                checked_at=_utc_now(),
                latency_ms=latency_ms,
                efficiency=efficiency,
                efficiency_unit=eff_unit,
                detail="Probe completed through OpenClaw.",
                reply_preview=reply_preview,
                http_status=None,
            )
        detail = _clean_text(data.get("error") or data.get("status") or "OpenClaw probe failed.")
        if status == "timeout":
            detail = detail or "OpenClaw probe timed out."
        return fail(detail)
    finally:
        try:
            gateway_call(
                openclaw_bin,
                "sessions.delete",
                {"key": session_key},
                timeout_ms=max(1000, timeout_ms // 2),
            )
        except Exception:
            pass


def _extract_gateway_error(result: Any) -> str:
    data = result.data if hasattr(result, "data") else None
    if isinstance(data, dict):
        for key in ("error", "message", "status"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for inner in ("message", "detail", "type"):
                    inner_val = value.get(inner)
                    if isinstance(inner_val, str) and inner_val.strip():
                        return inner_val.strip()
    stderr = getattr(result, "raw_stderr", "")
    stdout = getattr(result, "raw_stdout", "")
    return _clean_text(stderr or stdout)


def _combine_status(direct: Optional[ProbeResult], openclaw: Optional[ProbeResult]) -> Tuple[str, str, str]:
    available = [probe for probe in (direct, openclaw) if probe is not None]
    if not available:
        return "unknown", "unknown", "No probes executed."
    if all(probe.status == "ok" for probe in available):
        return "ok", "healthy", "All enabled probes succeeded."
    if any(probe.status == "ok" for probe in available):
        return "degraded", "degraded", "At least one probe path succeeded, but another probe path failed."
    first = sorted(available, key=lambda probe: _status_rank(probe.status))[0]
    detail = first.detail or "Probe failed."
    return first.status, _connection_state(first.status), detail


def _probe_target(
    target: ModelTarget,
    *,
    openclaw_root: Path,
    openclaw_bin: str,
    options: ModelProbeOptions,
) -> ModelRow:
    direct = probe_direct(target, prompt=options.prompt, timeout_seconds=options.timeout_seconds) if options.include_direct else None
    openclaw = (
        probe_openclaw(
            target,
            openclaw_root=openclaw_root,
            openclaw_bin=openclaw_bin,
            prompt=options.prompt,
            timeout_seconds=options.timeout_seconds,
        )
        if options.include_openclaw
        else None
    )
    overall_status, overall_connection, summary = _combine_status(direct, openclaw)
    return ModelRow(
        target=target,
        direct=direct,
        openclaw=openclaw,
        overall_status=overall_status,
        overall_connection=overall_connection,
        summary=summary,
    )


def collect_model_rows(
    *,
    openclaw_root: Path,
    openclaw_bin: str,
    options: Optional[ModelProbeOptions] = None,
    progress: Optional[Callable[[str, int, int], None]] = None,
) -> List[ModelRow]:
    opts = options or ModelProbeOptions()
    targets = discover_model_targets(openclaw_root)
    total = len(targets)
    if progress:
        progress("Loading model configuration...", 0, max(1, total))
    if not targets:
        return []

    rows: List[Optional[ModelRow]] = [None] * total
    max_workers = max(1, min(int(opts.max_workers), total))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(_probe_target, target, openclaw_root=openclaw_root, openclaw_bin=openclaw_bin, options=opts): idx
            for idx, target in enumerate(targets)
        }
        done = 0
        for future in as_completed(future_map):
            idx = future_map[future]
            rows[idx] = future.result()
            done += 1
            if progress:
                target = targets[idx]
                progress(f"Probing {target.agent_id}:{target.model_ref}", done, total)

    out = [row for row in rows if row is not None]
    out.sort(key=lambda row: (_status_rank(row.overall_status), row.target.agent_id, row.target.provider_id, row.target.model_id))
    return out


def format_model_table(rows: Sequence[ModelRow]) -> str:
    if not rows:
        return "No configured models found."

    lines: List[str] = []
    header = (
        f"{_fit('STATE', 10)}  {_fit('AGENT', 16)}  {_fit('MODEL', 28)}  "
        f"{_fit('ROLE', 20)}  {_fit('API', 18)}  {_fit('DIRECT', 14)}  {_fit('CLAW', 14)}  DETAIL"
    )
    lines.append(header)
    for row in rows:
        direct_status = _probe_short(row.direct)
        claw_status = _probe_short(row.openclaw)
        detail = row.summary
        if row.overall_status != "ok":
            failed = row.openclaw if row.openclaw and row.openclaw.status != "ok" else row.direct
            if failed:
                detail = failed.detail or detail
        lines.append(
            f"{_fit(_display_status(row.overall_status), 10)}  "
            f"{_fit(row.target.agent_label, 16)}  "
            f"{_fit(row.target.model_ref, 28)}  "
            f"{_fit(','.join(row.target.roles), 20)}  "
            f"{_fit(row.target.api_kind, 18)}  "
            f"{_fit(direct_status, 14)}  "
            f"{_fit(claw_status, 14)}  "
            f"{_clean_text(detail, limit=96)}"
        )
    return "\n".join(lines)


def _probe_short(probe: Optional[ProbeResult]) -> str:
    if probe is None:
        return "-"
    if probe.latency_ms is not None:
        return f"{_display_status(probe.status)}/{probe.latency_ms}ms"
    return _display_status(probe.status)


def format_model_markdown(rows: Sequence[ModelRow]) -> str:
    if not rows:
        return "_No configured models found._"
    lines = [
        "| State | Agent | Model | Roles | API | Direct | OpenClaw | Checked | Summary |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        checked_at = row.direct.checked_at if row.direct else (row.openclaw.checked_at if row.openclaw else None)
        lines.append(
            "| "
            + " | ".join(
                [
                    _display_status(row.overall_status),
                    row.target.agent_label,
                    row.target.model_ref,
                    ",".join(row.target.roles),
                    row.target.api_kind,
                    _probe_short(row.direct),
                    _probe_short(row.openclaw),
                    _fmt_age(checked_at),
                    _clean_text(row.summary, limit=80).replace("|", "\\|"),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def format_model_json(rows: Sequence[ModelRow], *, openclaw_root: Path, options: Optional[ModelProbeOptions] = None) -> str:
    opts = options or ModelProbeOptions()
    payload = {
        "openclawRoot": str(openclaw_root),
        "prompt": opts.prompt,
        "timeoutSeconds": opts.timeout_seconds,
        "includeDirect": opts.include_direct,
        "includeOpenClaw": opts.include_openclaw,
        "count": len(rows),
        "rows": [row.to_dict() for row in rows],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
