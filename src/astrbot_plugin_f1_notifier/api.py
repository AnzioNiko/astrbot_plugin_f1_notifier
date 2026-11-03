"""F1 API client module.

Wraps:
  - Jolpica-F1 (Ergast mirror): https://api.jolpi.ca/ergast/f1/
  - OpenF1: https://api.openf1.org/v1/

All public functions return ``ApiResult[T]`` — either ``Success(value=...)``
or ``Failure(error=...)``.  Callers use ``match`` / ``case`` to branch:

    result = await get_race_result()
    match result:
        case Success(value=race):  ...
        case Failure(error=err):   ...
"""

from __future__ import annotations

import asyncio
import json
import time
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from .models import (
    F1QualifyingResult,
    F1RaceResult,
    F1RaceWeekend,
    F1SessionSlot,
    F1SprintResult,
    Failure,
    JolpicaConstructorStanding,
    JolpicaDriverStanding,
    OpenF1Driver,
    OpenF1Meeting,
    OpenF1Position,
    OpenF1Session,
    OpenF1SessionResult,
    Success,
)

if TYPE_CHECKING:
    from .models import (
        ConstructorStandingsResult,
        DriversResult,
        GridResult,
        MeetingResult,
        RaceResult,
        ScheduleResult,
        SessionResult,
        SessionResultsResult,
        StandingsResult,
    )

JOLPICA_BASE = "https://api.jolpi.ca/ergast/f1"
OPENF1_BASE = "https://api.openf1.org/v1"

_SESSION_LOCK: asyncio.Lock | None = None
_SESSION_LOCK_LOOP: asyncio.AbstractEventLoop | None = None
_CLIENT_SESSION: aiohttp.ClientSession | None = None
_OPENF1_API_TOKEN: str = ""

# 内存缓存：{缓存键: (过期时间戳, 响应数据)}
_API_CACHE: dict[str, tuple[float, Any]] = {}
_API_CACHE_FILE: Path | None = None
_FORCE_REFRESH_API_CACHE: ContextVar[bool] = ContextVar(
    "f1_force_refresh_api_cache", default=False
)


def configure_persistent_cache(cache_file: str | Path) -> None:
    """配置并加载 API 响应的本地持久缓存文件。"""
    global _API_CACHE_FILE
    _API_CACHE_FILE = Path(cache_file)
    try:
        _API_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not _API_CACHE_FILE.exists():
            return
        payload = json.loads(_API_CACHE_FILE.read_text(encoding="utf-8"))
        entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
        now = time.time()
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            expiry = float(entry.get("expires_at", 0))
            if expiry > now and "data" in entry:
                _API_CACHE[key] = (expiry, entry["data"])
    except (OSError, ValueError, TypeError, AttributeError):
        # 缓存文件损坏或不可读时忽略它，后续请求仍可从网络重新建立缓存。
        return


def _persist_api_cache() -> None:
    """以原有 TTL 持久化尚未过期的 JSON 响应。"""
    if _API_CACHE_FILE is None:
        return
    now = time.time()
    entries: dict[str, dict[str, Any]] = {}
    for key, (expiry, data) in list(_API_CACHE.items()):
        if expiry <= now:
            _API_CACHE.pop(key, None)
            continue
        entries[key] = {"expires_at": expiry, "data": data}
    temporary_file = _API_CACHE_FILE.with_name(_API_CACHE_FILE.name + ".tmp")
    try:
        _API_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary_file.write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary_file.replace(_API_CACHE_FILE)
    except (OSError, TypeError, ValueError):
        # 本地存储失败不应影响 API 查询本身。
        try:
            temporary_file.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def force_refresh_cache():
    """强制跳过 API 缓存读取；成功取得的新响应仍会写入缓存。"""
    token = _FORCE_REFRESH_API_CACHE.set(True)
    try:
        yield
    finally:
        _FORCE_REFRESH_API_CACHE.reset(token)


def _get_cache_key(base: str, path: str, params: dict[str, Any] | None = None) -> str:
    """Generate a stable cache key string for a given request."""
    if not params:
        return f"{base}{path}"
    # Sort parameters to ensure consistent key for identical requests
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return f"{base}{path}?{query}"


def _ensure_lock() -> asyncio.Lock:
    """Return the session lock, recreating it when the event loop has changed."""
    global _SESSION_LOCK, _SESSION_LOCK_LOOP
    running_loop = asyncio.get_running_loop()
    if _SESSION_LOCK is None or _SESSION_LOCK_LOOP is not running_loop:
        _SESSION_LOCK = asyncio.Lock()
        _SESSION_LOCK_LOOP = running_loop
    return _SESSION_LOCK


def configure_openf1_api_token(token: str | None) -> None:
    """保存 OpenF1 API Token，令牌只会附加到 OpenF1 请求。"""
    global _OPENF1_API_TOKEN
    _OPENF1_API_TOKEN = token.strip() if isinstance(token, str) else ""


async def _get_session() -> aiohttp.ClientSession:
    global _CLIENT_SESSION
    lock = _ensure_lock()
    async with lock:
        if _CLIENT_SESSION is None or _CLIENT_SESSION.closed:
            _CLIENT_SESSION = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Accept": "application/json"},
            )
    return _CLIENT_SESSION


async def close_session() -> None:
    global _CLIENT_SESSION
    lock = _ensure_lock()
    async with lock:
        if _CLIENT_SESSION and not _CLIENT_SESSION.closed:
            await _CLIENT_SESSION.close()
            _CLIENT_SESSION = None


# ──────────────────────────────────────────────────────────────────────────────
# Low-level HTTP helpers (return raw data — no model parsing here)
# ──────────────────────────────────────────────────────────────────────────────


async def _jolpica_get(path: str) -> dict[str, Any]:
    cache_key = _get_cache_key(JOLPICA_BASE, path)
    now = time.time()
    if not _FORCE_REFRESH_API_CACHE.get() and cache_key in _API_CACHE:
        expiry, value = _API_CACHE[cache_key]
        if now < expiry:
            return value

    session = await _get_session()
    url = f"{JOLPICA_BASE}{path}"
    async with session.get(url) as resp:
        if resp.status == 429:
            # Simple back-off on rate limit
            await asyncio.sleep(5)
        resp.raise_for_status()
        data = await resp.json(content_type=None)
        # Cache Jolpica for 1 hour by default
        _API_CACHE[cache_key] = (now + 3600, data)
        _persist_api_cache()
        return data


async def _openf1_get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    bypass_cache: bool = False,
) -> list[dict[str, Any]]:
    cache_key = _get_cache_key(OPENF1_BASE, path, params)
    now = time.time()
    if (
        not bypass_cache
        and not _FORCE_REFRESH_API_CACHE.get()
        and cache_key in _API_CACHE
    ):
        expiry, value = _API_CACHE[cache_key]
        if now < expiry:
            return value

    # Determine TTL based on request specifics
    ttl = 60  # Default 1 minute
    p = params or {}
    if "meeting_key" in p or "session_key" in p:
        if path == "/session_result":
            # Session results change during session; cache short
            ttl = 300  # 5 minutes
        else:
            # Drivers, meetings, etc. are largely static once created
            ttl = 86400  # 24 hours
    elif "year" in p and path in ("/meetings", "/sessions"):
        # Full year schedule data doesn't change every minute
        ttl = 3600  # 1 hour

    session = await _get_session()
    url = f"{OPENF1_BASE}{path}"
    headers = (
        {"Authorization": f"Bearer {_OPENF1_API_TOKEN}"}
        if _OPENF1_API_TOKEN
        else None
    )
    async with session.get(url, params=params, headers=headers) as resp:
        if resp.status == 429:
            # Simple back-off on rate limit
            await asyncio.sleep(5)
        resp.raise_for_status()
        data = await resp.json(content_type=None)
        if not bypass_cache:
            _API_CACHE[cache_key] = (now + ttl, data)
            _persist_api_cache()
        return data


# ──────────────────────────────────────────────────────────────────────────────
# Datetime helpers
# ──────────────────────────────────────────────────────────────────────────────


def _parse_iso_datetime(s: str) -> datetime | None:
    """Parse ISO 8601 string → timezone-aware UTC datetime, or None."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


def _split_iso_dt(ds: str) -> tuple[str, str]:
    """Split an ISO 8601 datetime into ('YYYY-MM-DD', 'HH:MM:SSZ').

    Normalises to UTC so downstream code can stay simple.
    """
    dt = _parse_iso_datetime(ds)
    if dt is None:
        return ds[:10], ""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%d"), utc.strftime("%H:%M:%SZ")


async def _empty_list() -> list:
    return []


def _secs_to_laptime(secs: float | None) -> str:
    """Convert seconds → '1:23.456' qualifying lap-time string, or '—'."""
    if not secs:
        return "—"
    m = int(secs) // 60
    s = secs - m * 60
    return f"{m}:{s:06.3f}"


def _secs_to_racetime(secs: float | None) -> str:
    """Convert seconds → '1:23:45.678' (or '23:45.678') race-time string."""
    if not secs:
        return "-"
    h = int(secs) // 3600
    m = (int(secs) % 3600) // 60
    s = secs % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:06.3f}"
    return f"{m}:{s:06.3f}"


# Maps OpenF1 circuit_short_name → Jolpica circuit_id (for image lookup)
_OPENF1_CIRCUIT_MAP: dict[str, str] = {
    "Albert Park": "albert_park",
    "Melbourne": "albert_park",
    "Shanghai": "shanghai",
    "Suzuka": "suzuka",
    "Sakhir": "bahrain",
    "Bahrain": "bahrain",
    "Jeddah": "jeddah",
    "Miami": "miami",
    "Imola": "imola",
    "Monaco": "monaco",
    "Catalunya": "catalunya",
    "Barcelona": "catalunya",
    "Gilles Villeneuve": "villeneuve",
    "Montreal": "villeneuve",
    "Red Bull Ring": "red_bull_ring",
    "Spielberg": "red_bull_ring",
    "Silverstone": "silverstone",
    "Spa-Francorchamps": "spa",
    "Hungaroring": "hungaroring",
    "Budapest": "hungaroring",
    "Zandvoort": "zandvoort",
    "Monza": "monza",
    "Baku": "baku",
    "Singapore": "marina_bay",
    "Marina Bay": "marina_bay",
    "Austin": "americas",
    "COTA": "americas",
    "Mexico City": "rodriguez",
    "Interlagos": "interlagos",
    "São Paulo": "interlagos",
    "Las Vegas": "vegas",
    "Lusail": "losail",
    "Yas Marina": "yas_marina",
    "Portimão": "portimao",
    "Istanbul": "istanbul",
    "Mugello": "mugello",
    "Sochi": "sochi",
    "Nürburgring": "nurburgring",
}


async def _get_openf1_round(
    session_name: str, round_number: int | str, year: int
) -> tuple[dict, int]:
    """按 Jolpica 的赛程日期查找 OpenF1 场次，返回场次和零基轮次。"""
    sessions_raw = await _openf1_get(
        "/sessions", params={"session_name": session_name, "year": year}
    )
    schedule_result = await get_current_schedule(year)
    match schedule_result:
        case Success(value=races):
            pass
        case Failure(error=err):
            raise ValueError(f"无法取得 Jolpica 赛程以匹配 OpenF1 场次：{err}")

    now = datetime.now(timezone.utc)
    session_field = {
        "Race": None,
        "Qualifying": "qualifying",
        "Sprint": "sprint",
    }.get(session_name)
    if session_name not in {"Race", "Qualifying", "Sprint"}:
        raise ValueError(f"不支持的 OpenF1 场次类型：{session_name}")

    def _scheduled_start(race: F1RaceWeekend) -> datetime | None:
        if session_field is None:
            date_part, time_part = race.date, race.time
        else:
            slot = getattr(race, session_field)
            if slot is None:
                return None
            date_part, time_part = slot.date, slot.time
        dt = _parse_iso_datetime(f"{date_part}T{time_part}")
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    scheduled: list[tuple[F1RaceWeekend, datetime]] = []
    for race in races:
        start = _scheduled_start(race)
        if start is not None and start <= now:
            scheduled.append((race, start))
    if not scheduled:
        raise ValueError(f"no past {session_name!r} sessions for {year}")

    if round_number == "last":
        target_race, expected_start = max(scheduled, key=lambda item: item[1])
    else:
        try:
            target_round = int(round_number)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid round number: {round_number}") from exc
        match_round = next(
            ((race, start) for race, start in scheduled if race.round_int == target_round),
            None,
        )
        if match_round is None:
            raise ValueError(
                f"round {round_number} has no past {session_name!r} session in {year}"
            )
        target_race, expected_start = match_round

    # Jolpica 与 OpenF1 的赛历可能因取消或调整站次而数量不同，不能按列表下标对应。
    candidates: list[tuple[timedelta, dict]] = []
    for session in sessions_raw:
        session_start = _parse_iso_datetime(session.get("date_start", ""))
        if session_start is None:
            continue
        if session_start.tzinfo is None:
            session_start = session_start.replace(tzinfo=timezone.utc)
        session_start = session_start.astimezone(timezone.utc)
        if session_start > now:
            continue
        distance = abs(session_start - expected_start)
        if distance <= timedelta(hours=24):
            candidates.append((distance, session))

    if not candidates:
        raise ValueError(
            f"no OpenF1 {session_name!r} session matches round "
            f"{target_race.round_int} near {expected_start.isoformat()}"
        )

    chosen = min(candidates, key=lambda item: item[0])[1]
    return chosen, target_race.round_int - 1

async def _qualifying_from_openf1(
    round_number: int | str, year: int
) -> "RaceResult":
    """Fetch qualifying result from OpenF1."""
    chosen, chosen_idx = await _get_openf1_round("Qualifying", round_number, year)
    session_key = chosen["session_key"]

    meeting_key = chosen.get("meeting_key")
    results_raw, drivers_raw, meetings_raw = await asyncio.gather(
        _openf1_get("/session_result", params={"session_key": session_key}),
        _openf1_get("/drivers", params={"session_key": session_key}),
        _openf1_get("/meetings", params={"meeting_key": meeting_key})
        if meeting_key
        else _empty_list(),
    )
    if not results_raw or not drivers_raw:
        raise ValueError("empty session_result or drivers from OpenF1")

    drivers_map: dict[int, dict] = {d["driver_number"]: d for d in drivers_raw}
    meeting: dict = meetings_raw[0] if meetings_raw else {}

    qual_results: list[F1QualifyingResult] = []
    for entry in sorted(results_raw, key=lambda x: x.get("position") or 99):
        driver_num = entry.get("driver_number")
        d = drivers_map.get(driver_num, {})

        duration = entry.get("duration")
        if isinstance(duration, list):
            q1_s = duration[0] if len(duration) > 0 else None
            q2_s = duration[1] if len(duration) > 1 else None
            q3_s = duration[2] if len(duration) > 2 else None
        else:
            q1_s, q2_s, q3_s = None, None, None

        first_name = d.get("first_name") or ""
        last_name = d.get("last_name") or f"#{driver_num}"
        qual_results.append(
            F1QualifyingResult(
                position=entry.get("position"),
                driver_name=f"{first_name} {last_name}".strip(),
                driver_first_name=first_name,
                driver_last_name=last_name,
                team_name=d.get("team_name") or "?",
                headshot_url=d.get("headshot_url") or None,
                q1=_secs_to_laptime(q1_s),
                q2=_secs_to_laptime(q2_s),
                q3=_secs_to_laptime(q3_s),
            )
        )

    circuit_short = chosen.get("circuit_short_name") or ""
    return Success(
        value=F1RaceWeekend(
            season=str(year),
            round=str(chosen_idx + 1),
            race_name=meeting.get("meeting_name") or chosen.get("location") or "?",
            circuit_id=_OPENF1_CIRCUIT_MAP.get(circuit_short, ""),
            circuit_name=circuit_short,
            locality=chosen.get("location") or "",
            country=chosen.get("country_name") or "",
            meeting_key=chosen.get("meeting_key") or 0,
            date=chosen.get("date_start", "")[:10],
            qualifying_results=qual_results,
        )
    )


async def _race_from_openf1(
    round_number: int | str, year: int
) -> "RaceResult":
    """Fetch race result from OpenF1."""
    chosen, chosen_idx = await _get_openf1_round("Race", round_number, year)
    session_key = chosen["session_key"]

    meeting_key = chosen.get("meeting_key")
    results_raw, drivers_raw, meetings_raw = await asyncio.gather(
        _openf1_get("/session_result", params={"session_key": session_key}),
        _openf1_get("/drivers", params={"session_key": session_key}),
        _openf1_get("/meetings", params={"meeting_key": meeting_key})
        if meeting_key
        else _empty_list(),
    )
    if not results_raw or not drivers_raw:
        raise ValueError("empty session_result or drivers from OpenF1")

    drivers_map: dict[int, dict] = {d["driver_number"]: d for d in drivers_raw}
    meeting: dict = meetings_raw[0] if meetings_raw else {}

    results_sorted = sorted(results_raw, key=lambda x: x.get("position") or 99)
    leader_duration = results_sorted[0].get("duration") if results_sorted else None

    race_results: list[F1RaceResult] = []
    for entry in results_sorted:
        driver_num = entry.get("driver_number")
        d = drivers_map.get(driver_num, {})
        pos = entry.get("position")

        if entry.get("dns"):
            status, time_val = "DNS", None
        elif entry.get("dsq"):
            status, time_val = "DSQ", None
        elif entry.get("dnf"):
            status, time_val = "DNF", None
        else:
            status = "Finished"
            gap = entry.get("gap_to_leader")
            if pos == 1:
                time_val = _secs_to_racetime(leader_duration)
            elif isinstance(gap, str):
                time_val = gap
            elif isinstance(gap, (int, float)) and gap > 0:
                time_val = f"+{gap:.3f}"
            else:
                time_val = "-"

        race_pts = int(entry.get("points") or 0)
        first_name = d.get("first_name") or ""
        last_name = d.get("last_name") or f"#{driver_num}"

        race_results.append(
            F1RaceResult(
                position=pos,
                driver_name=f"{first_name} {last_name}".strip(),
                driver_first_name=first_name,
                driver_last_name=last_name,
                team_name=d.get("team_name") or "?",
                headshot_url=d.get("headshot_url") or None,
                laps=str(entry.get("number_of_laps") or ""),
                status=status,
                time=time_val,
                points=str(race_pts),
            )
        )

    circuit_short = chosen.get("circuit_short_name") or ""
    return Success(
        value=F1RaceWeekend(
            season=str(year),
            round=str(chosen_idx + 1),
            race_name=meeting.get("meeting_name") or chosen.get("location") or "?",
            circuit_id=_OPENF1_CIRCUIT_MAP.get(circuit_short, ""),
            circuit_name=circuit_short,
            locality=chosen.get("location") or "",
            country=chosen.get("country_name") or "",
            meeting_key=chosen.get("meeting_key") or 0,
            date=chosen.get("date_start", "")[:10],
            race_results=race_results,
        )
    )


async def _sprint_from_openf1(
    round_number: int | str, year: int
) -> "RaceResult":
    """Fetch sprint race result from OpenF1."""
    chosen, chosen_idx = await _get_openf1_round("Sprint", round_number, year)
    session_key = chosen["session_key"]

    meeting_key = chosen.get("meeting_key")
    results_raw, drivers_raw, meetings_raw = await asyncio.gather(
        _openf1_get("/session_result", params={"session_key": session_key}),
        _openf1_get("/drivers", params={"session_key": session_key}),
        _openf1_get("/meetings", params={"meeting_key": meeting_key})
        if meeting_key
        else _empty_list(),
    )
    if not results_raw or not drivers_raw:
        raise ValueError("empty session_result or drivers from OpenF1")

    drivers_map: dict[int, dict] = {d["driver_number"]: d for d in drivers_raw}
    meeting: dict = meetings_raw[0] if meetings_raw else {}

    results_sorted = sorted(results_raw, key=lambda x: x.get("position") or 99)
    leader_duration = results_sorted[0].get("duration") if results_sorted else None

    sprint_results: list[F1SprintResult] = []
    for entry in results_sorted:
        driver_num = entry.get("driver_number")
        d = drivers_map.get(driver_num, {})
        pos = entry.get("position")

        if entry.get("dns"):
            status, time_val = "DNS", None
        elif entry.get("dsq"):
            status, time_val = "DSQ", None
        elif entry.get("dnf"):
            status, time_val = "DNF", None
        else:
            status = "Finished"
            gap = entry.get("gap_to_leader")
            if pos == 1:
                time_val = _secs_to_racetime(leader_duration)
            elif isinstance(gap, str):
                time_val = gap
            elif isinstance(gap, (int, float)) and gap > 0:
                time_val = f"+{gap:.3f}"
            else:
                time_val = "-"

        first_name = d.get("first_name") or ""
        last_name = d.get("last_name") or f"#{driver_num}"

        sprint_results.append(
            F1SprintResult(
                position=pos,
                driver_name=f"{first_name} {last_name}".strip(),
                driver_first_name=first_name,
                driver_last_name=last_name,
                team_name=d.get("team_name") or "?",
                headshot_url=d.get("headshot_url") or None,
                laps=str(entry.get("number_of_laps") or ""),
                status=status,
                time=time_val,
                points=str(int(entry.get("points") or 0)),
            )
        )

    circuit_short = chosen.get("circuit_short_name") or ""
    return Success(
        value=F1RaceWeekend(
            season=str(year),
            round=str(chosen_idx + 1),
            race_name=meeting.get("meeting_name") or chosen.get("location") or "?",
            circuit_id=_OPENF1_CIRCUIT_MAP.get(circuit_short, ""),
            circuit_name=circuit_short,
            locality=chosen.get("location") or "",
            country=chosen.get("country_name") or "",
            meeting_key=chosen.get("meeting_key") or 0,
            date=chosen.get("date_start", "")[:10],
            sprint_results=sprint_results,
        )
    )


async def get_current_schedule(season: int | str = "current") -> ScheduleResult:
    """使用 Jolpica 返回赛程，避免轮询 OpenF1 赛程接口。"""
    try:
        year = datetime.now(timezone.utc).year if season == "current" else int(season)
        raw = await _jolpica_get(f"/{year}/races.json?limit=100")
        races: list[dict[str, Any]] = raw["MRData"]["RaceTable"]["Races"]
        if not races:
            return Failure(error="empty schedule")

        session_fields = {
            "FirstPractice": "first_practice",
            "SecondPractice": "second_practice",
            "ThirdPractice": "third_practice",
            "Qualifying": "qualifying",
            "Sprint": "sprint",
            "SprintQualifying": "sprint_qualifying",
        }
        weekends: list[F1RaceWeekend] = []
        for idx, race in enumerate(races, start=1):
            circuit = race.get("Circuit") or {}
            location = circuit.get("Location") or {}
            kwargs: dict[str, Any] = {
                "season": str(race.get("season") or year),
                "round": str(race.get("round") or idx),
                "race_name": race.get("raceName") or "",
                "circuit_id": circuit.get("circuitId") or "",
                "circuit_name": circuit.get("circuitName") or "",
                "locality": location.get("locality") or "",
                "country": location.get("country") or "",
                "date": race.get("date") or "",
                "time": race.get("time") or "",
            }
            for api_field, model_field in session_fields.items():
                session = race.get(api_field)
                if not isinstance(session, dict):
                    continue
                session_date = session.get("date") or ""
                session_time = session.get("time") or ""
                if session_date and session_time:
                    kwargs[model_field] = F1SessionSlot(
                        date=session_date,
                        time=session_time,
                    )
            weekends.append(F1RaceWeekend(**kwargs))

        return Success(value=weekends)
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))


async def get_race_result(
    round_number: int | str = "last", season: int | str = "current"
) -> RaceResult:
    """Return race result from OpenF1."""
    year = datetime.now(timezone.utc).year if season == "current" else int(season)
    try:
        return await _race_from_openf1(round_number, year)
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))


async def get_qualifying_result(
    round_number: int | str = "last", season: int | str = "current"
) -> RaceResult:
    """Return qualifying result from OpenF1."""
    year = datetime.now(timezone.utc).year if season == "current" else int(season)
    try:
        return await _qualifying_from_openf1(round_number, year)
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))


async def get_sprint_result(
    round_number: int | str, season: int | str = "current"
) -> RaceResult:
    """Return sprint race result from OpenF1."""
    year = datetime.now(timezone.utc).year if season == "current" else int(season)
    try:
        return await _sprint_from_openf1(round_number, year)
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))


async def get_driver_standings(season: int | str = "current") -> StandingsResult:
    """Return driver championship standings for a season."""
    try:
        raw = await _jolpica_get(f"/{season}/driverStandings.json")
        tables: list[dict] = raw["MRData"]["StandingsTable"]["StandingsLists"]
        match tables:
            case [first, *_]:
                standings = [
                    JolpicaDriverStanding.model_validate(e)
                    for e in first["DriverStandings"]
                ]

                # Jolpica 的积分榜没有头像字段。直接调用 OpenF1 的
                # /drivers?session_key=latest 接口取得车手资料，并按姓名/缩写
                # 进行匹配；头像补充失败时不影响积分榜本身返回。
                try:
                    headshots = await _get_driver_headshots()
                    for entry in standings:
                        driver = entry.driver
                        for candidate in (
                            driver.driver_id,
                            driver.full_name,
                            driver.family_name,
                        ):
                            url = headshots.get(_normalise_driver_text(candidate))
                            if url:
                                driver.headshot_url = url
                                break
                except Exception:
                    # 头像只是增强信息，OpenF1 不可用时仍然显示完整榜单。
                    pass

                return Success(value=standings)
            case _:
                return Failure(error="no standings data")
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))


def _normalise_driver_text(value: str | None) -> str:
    """将车手姓名转换为可跨接口匹配的键。"""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return "".join(char for char in without_marks.casefold() if char.isalnum())


async def _get_driver_headshots() -> dict[str, str]:
    """直接从 OpenF1 车手接口获取头像，并建立姓名匹配表。

    使用 ``session_key=latest`` 直接请求车手资料，不再先查询最近一次
    正赛，也不依赖正赛是否已经结束或是否存在正赛记录。
    """
    drivers_result = await get_drivers_for_session("latest")

    match drivers_result:
        case Success(value=drivers):
            lookup: dict[str, str] = {}
            for driver in drivers:
                if not driver.headshot_url:
                    continue
                for candidate in (
                    driver.name_acronym,
                    driver.full_name,
                    driver.last_name,
                ):
                    key = _normalise_driver_text(candidate)
                    if key:
                        lookup[key] = driver.headshot_url
            return lookup
        case Failure():
            return {}


async def get_constructor_standings(
    season: int | str = "current",
) -> ConstructorStandingsResult:
    """Return constructor championship standings for a season."""
    try:
        raw = await _jolpica_get(f"/{season}/constructorStandings.json")
        tables: list[dict] = raw["MRData"]["StandingsTable"]["StandingsLists"]
        match tables:
            case [first, *_]:
                standings = [
                    JolpicaConstructorStanding.model_validate(e)
                    for e in first["ConstructorStandings"]
                ]
                return Success(value=standings)
            case _:
                return Failure(error="no constructor standings data")
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# OpenF1 public API
# ──────────────────────────────────────────────────────────────────────────────


async def get_latest_session(session_name: str = "Race") -> SessionResult:
    """Return the most recently *started* OpenF1 session with given name.

    Filters out sessions that haven't started yet so we never return a future
    session_key (e.g. Abu Dhabi when the current race is Australia).
    """
    try:
        year = datetime.now(timezone.utc).year
        results = await _openf1_get(
            "/sessions",
            params={"session_name": session_name, "year": year},
        )
        now = datetime.now(timezone.utc)
        past = [
            r
            for r in results
            if (dt := _parse_iso_datetime(r.get("date_start", ""))) is not None
            and dt <= now
        ]
        match past:
            case []:
                return Failure(error="no past sessions found")
            case past_list:
                past_list.sort(
                    key=lambda r: (
                        _parse_iso_datetime(r.get("date_start", ""))
                        or datetime.min.replace(tzinfo=timezone.utc)
                    )
                )
                return Success(value=OpenF1Session.model_validate(past_list[-1]))
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))


# Maps user-friendly fp_number → OpenF1 session_name
FP_SESSION_NAMES = {
    "1": "Practice 1",
    "fp1": "Practice 1",
    "2": "Practice 2",
    "fp2": "Practice 2",
    "3": "Practice 3",
    "fp3": "Practice 3",
}


async def get_practice_session(
    fp_number: str = "1",
    year: int | None = None,
    *,
    bypass_cache: bool = False,
) -> SessionResult:
    """返回 OpenF1 中最近开始的对应练习赛场次。

    ``bypass_cache`` 用于调度器定期检查场次结束状态，
    避免缓存导致无法看到更新后的 ``date_end``。
    """
    try:
        session_name = FP_SESSION_NAMES.get(fp_number.lower(), "Practice 1")
        if year is None:
            year = datetime.now(timezone.utc).year
        results = await _openf1_get(
            "/sessions",
            params={"session_name": session_name, "year": year},
            bypass_cache=bypass_cache,
        )
        now = datetime.now(timezone.utc)
        past = [
            r
            for r in results
            if (dt := _parse_iso_datetime(r.get("date_start", ""))) is not None
            and dt <= now
        ]
        match past:
            case []:
                return Failure(error=f"no past FP{fp_number} session found")
            case past_list:
                past_list.sort(
                    key=lambda r: (
                        _parse_iso_datetime(r.get("date_start", ""))
                        or datetime.min.replace(tzinfo=timezone.utc)
                    )
                )
                return Success(value=OpenF1Session.model_validate(past_list[-1]))
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))

async def get_drivers_for_session(session_key: int | str) -> DriversResult:
    """Return driver info for a given OpenF1 session key."""
    try:
        raw = await _openf1_get("/drivers", params={"session_key": session_key})
        drivers = [OpenF1Driver.model_validate(d) for d in raw]
        return Success(value=drivers)
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))


async def get_starting_grid(session_key: int | str) -> GridResult:
    """Return starting grid sorted by position using OpenF1 /starting_grid."""
    try:
        raw = await _openf1_get(
            "/starting_grid", params={"session_key": session_key}
        )
        if not raw:
            return Success(value=[])
        grid = sorted(
            (OpenF1Position.model_validate(e) for e in raw),
            key=lambda x: x.position,
        )
        return Success(value=grid)
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))


async def get_meeting_for_session(session_key: int | str) -> MeetingResult:
    """Return meeting info for an OpenF1 session key."""
    try:
        # First resolve the meeting_key from the session, then query meetings
        sessions = await _openf1_get("/sessions", params={"session_key": session_key})
        if not sessions:
            return Failure(error="no session found")
        meeting_key = sessions[0].get("meeting_key")
        if not meeting_key:
            return Failure(error="session has no meeting_key")
        results = await _openf1_get("/meetings", params={"meeting_key": meeting_key})
        match results:
            case [first, *_]:
                return Success(value=OpenF1Meeting.model_validate(first))
            case _:
                return Failure(error="no meeting found")
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))


async def get_session_result(session_key: int | str) -> SessionResultsResult:
    """Return session result (standings) for a given OpenF1 session.

    Each entry: position, driver_number, duration (best lap s), gap_to_leader,
    number_of_laps.  Sorted by position ascending.
    """
    try:
        raw = await _openf1_get("/session_result", params={"session_key": session_key})
        results = sorted(
            (OpenF1SessionResult.model_validate(r) for r in raw),
            key=lambda x: x.position,
        )
        return Success(value=results)
    except (
        aiohttp.ClientError,
        KeyError,
        ValueError,
        TypeError,
        asyncio.TimeoutError,
    ) as exc:
        return Failure(error=str(exc))








