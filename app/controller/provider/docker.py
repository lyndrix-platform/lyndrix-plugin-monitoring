import httpx
from typing import Any, Dict, List, Optional

from ...model.models import MonitorState
from ._ssrf import ProbeTargetError, assert_host_allowed


def is_docker_host(metadata: Dict[str, Any]) -> bool:
    groups = [str(g).lower() for g in (metadata.get("groups") or [])]
    roles = [str(r).lower() for r in (metadata.get("baseline_roles") or [])]
    return any(item in {"docker", "docker_hosts"} for item in groups + roles)


def normalize_docker_name(value: Optional[str]) -> str:
    if not value:
        return ""
    normalized = str(value).strip().lower().replace("_", "-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized.strip("-/ ")


def docker_name_candidates(service_name: str) -> List[str]:
    normalized = normalize_docker_name(service_name)
    if not normalized:
        return []
    candidates = [normalized]
    if normalized.startswith("aac-"):
        candidates.append(normalized.split("aac-", 1)[1])
    if normalized.startswith("service-"):
        candidates.append(normalized.split("service-", 1)[1])
    parts = [p for p in normalized.split("-") if p]
    if len(parts) > 1:
        candidates.append("-".join(parts[1:]))
        candidates.append(parts[-1])
    unique: List[str] = []
    for item in candidates:
        if item and item not in unique:
            unique.append(item)
    return unique


def docker_container_score(container: Dict[str, Any], service_name: str) -> int:
    candidates = docker_name_candidates(service_name)
    if not candidates:
        return 0
    names = [n.lstrip("/") for n in (container.get("Names") or [])]
    labels = container.get("Labels") or {}
    haystacks = names + [
        container.get("Id", ""),
        labels.get("com.docker.compose.service", ""),
        labels.get("com.docker.compose.project", ""),
        labels.get("com.docker.compose.container-number", ""),
        labels.get("com.docker.swarm.service.name", ""),
    ]
    normalized_haystacks = [normalize_docker_name(h) for h in haystacks if h]
    best = 0
    for candidate in candidates:
        for haystack in normalized_haystacks:
            if not haystack:
                continue
            if haystack == candidate:
                best = max(best, 100)
            elif haystack.endswith(f"-{candidate}"):
                best = max(best, 90)
            elif f"-{candidate}-" in haystack:
                best = max(best, 85)
            elif candidate in haystack:
                best = max(best, 70)
    return best


async def run_docker_service_probe(
    client: httpx.AsyncClient, host: str, service_name: str, timeout_seconds: int
) -> Dict[str, Any]:
    # SSRF guard: the Docker API (plaintext :2375) requires a trusted endpoint;
    # reject link-local/metadata hosts before forging the request.
    try:
        assert_host_allowed(host)
    except ProbeTargetError as exc:
        return {"state": MonitorState.DOWN, "latency_ms": None, "error_message": str(exc)}
    endpoint = f"http://{host}:2375/containers/json"
    response = await client.get(endpoint, params={"all": 1}, timeout=httpx.Timeout(timeout_seconds))
    latency_ms = round(response.elapsed.total_seconds() * 1000.0, 2)
    if response.status_code >= 400:
        return {
            "state": MonitorState.DOWN,
            "latency_ms": latency_ms,
            "error_message": f"Docker API HTTP {response.status_code}",
        }
    containers = response.json()
    matched = None
    matched_score = 0
    for container in containers:
        score = docker_container_score(container, service_name)
        if score > matched_score:
            matched = container
            matched_score = score
    if not matched:
        return {
            "state": MonitorState.DOWN,
            "latency_ms": latency_ms,
            "error_message": f"Container '{service_name}' not found on Docker host",
        }
    state = (matched.get("State") or "unknown").lower()
    status = matched.get("Status") or state
    if state == "running":
        return {"state": MonitorState.UP, "latency_ms": latency_ms, "error_message": None}
    if state in {"paused", "restarting", "created", "exited", "dead", "removing"}:
        return {
            "state": MonitorState.DOWN,
            "latency_ms": latency_ms,
            "error_message": f"Docker container state: {status}",
        }
    return {
        "state": MonitorState.UNKNOWN,
        "latency_ms": latency_ms,
        "error_message": f"Docker container state: {status}",
    }
