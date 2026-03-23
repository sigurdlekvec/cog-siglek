"""
Garmin Activities Extractor - Ingests Garmin Connect activities into CDF.

Authentication (in order of preference):
1) Environment variable GARMINTOKENS — long base64 session from `garth` / local login (required for
   Cognite Functions: no ~/.garminconnect disk, and password-only login often fails with
   "OAuth1 token is required for OAuth2 refresh" on MFA or fresh garth sessions).
2) Secrets garmin-email + garmin-password — may work for non-MFA accounts with current garminconnect/garth.

Creates:
- Events: workout summaries (upsert by external_id)
- Data Model instances: Workout nodes in garmin_coach_space
- Time Series: high-resolution HR, pace, cadence from TCX when available

Configure in Fusion after deploy: env GARMINTOKENS and/or secrets garmin-email, garmin-password, garmin-tokens (see README).

Data parameters (function call / schedule):
- days_back: calendar lookback from today (default 7). Use ~1095 for ~3 years initial load.
- chunk_days: split API calls into windows of this many days (default 30 for large backfills, else single request).
- include_tcx: download TCX for time series (default True). For multi-year backfills set False to avoid timeouts.
- space_name: CDF space for Workout instances (default garmin_coach_space).
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
from datetime import datetime, timedelta, timezone
import tempfile
from typing import Any

from cognite.client import CogniteClient
from cognite.client.data_classes import EventWrite, ExtractionPipelineRunWrite
from cognite.client.data_classes.data_modeling import NodeApply
from cognite.client.exceptions import CogniteAPIError

from cognite.client.config import global_config

global_config.disable_pypi_version_check = True

# Constants
EXTERNAL_ID_PREFIX = "garmin_activity_"
EXTRACTION_PIPELINE_ID = "ep_garmin_activities"
DEFAULT_DAYS_BACK = 7
# When days_back exceeds this, default chunk_days=30 and include_tcx=False unless overridden
LARGE_BACKFILL_THRESHOLD_DAYS = 90
DEFAULT_CHUNK_DAYS_LARGE = 30


def _safe_float(val: Any) -> float | None:
    """Extract float from Garmin API response, handling nested structures."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        return _safe_float(val.get("value") or val.get("valueInUnit"))
    return None


def _parse_garmin_timestamp(ts: str | None) -> int | None:
    """Parse Garmin timestamp (ISO or ms) to milliseconds since epoch."""
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        return int(ts) if ts > 1e12 else int(ts) * 1000
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def extract_summary(act: dict) -> dict:
    """Extract workout summary from Garmin activity dict."""
    activity_id = str(act.get("activityId", act.get("activityUUID", "")))
    start_time = _parse_garmin_timestamp(
        act.get("startTimeGMT") or act.get("startTime") or act.get("beginTimestamp")
    )
    duration_ms = _safe_float(act.get("duration")) or 0
    duration_seconds = duration_ms / 1000.0 if duration_ms > 1000 else duration_ms
    distance_m = _safe_float(act.get("distance"))
    distance_km = (distance_m / 1000.0) if distance_m is not None else None
    avg_speed = _safe_float(act.get("averageSpeed"))  # m/s typically
    avg_pace = (1000.0 / (avg_speed * 60)) if avg_speed and avg_speed > 0 else None  # min/km

    return {
        "activity_id": activity_id,
        "activity_type": (act.get("activityType", {}).get("typeKey", "") or act.get("activityType", "") or "").lower(),
        "activity_name": act.get("activityName", ""),
        "start_time": start_time,
        "duration_seconds": duration_seconds,
        "distance_km": distance_km,
        "avg_heart_rate": _safe_float(act.get("averageHR") or act.get("averageHeartRate")),
        "max_heart_rate": _safe_float(act.get("maxHR") or act.get("maxHeartRate")),
        "avg_pace_min_per_km": avg_pace,
        "calories": _safe_float(act.get("calories")),
        "elevation_gain_m": _safe_float(act.get("elevationGain") or act.get("gain")),
    }


def report_extraction_pipeline(client: CogniteClient, status: str, message: str | None = None) -> None:
    """Report extraction pipeline run status."""
    try:
        # Use ExtractionPipelineRunWrite for creates — ExtractionPipelineRun is the read model and
        # requires id in current cognite-sdk (raises TypeError if used as write payload).
        run = ExtractionPipelineRunWrite(
            extpipe_external_id=EXTRACTION_PIPELINE_ID,
            status=status,
            message=message,
        )
        client.extraction_pipelines.runs.create(run=run)
    except CogniteAPIError as e:
        if e.code == 403:
            print(f"Warning: Cannot report extraction pipeline status - {e.message}")
        else:
            raise


def parse_tcx_to_timeseries(tcx_bytes: bytes, activity_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Parse TCX bytes and return (heart_rate_datapoints, pace_datapoints, cadence_datapoints).
    Each list contains dicts with 'timestamp' (ms) and 'value'.
    """
    try:
        from tcxparser import TCXParser
    except ImportError:
        return [], [], []

    with tempfile.NamedTemporaryFile(suffix=".tcx", delete=False) as f:
        f.write(tcx_bytes)
        f.flush()
        try:
            tcx = TCXParser(f.name)
        except Exception:
            return [], [], []
        finally:
            import os
            try:
                os.unlink(f.name)
            except OSError:
                pass

    hr_values = tcx.hr_values()
    time_objs = tcx.time_objects()
    cadence_values = tcx.cadence_values()
    dist_elems = tcx.distance_values()
    distance_values = []
    for d in dist_elems:
        try:
            distance_values.append(float(d.text) if d.text else 0.0)
        except (ValueError, TypeError):
            distance_values.append(0.0)

    hr_dps = []
    pace_dps = []
    cadence_dps = []
    n = len(time_objs)

    for i in range(n):
        ts = time_objs[i]
        ts_ms = int(ts.timestamp() * 1000)

        if i < len(hr_values) and hr_values[i] is not None:
            hr_dps.append({"timestamp": ts_ms, "value": float(hr_values[i])})

        if i < len(cadence_values) and cadence_values[i] is not None:
            cadence_dps.append({"timestamp": ts_ms, "value": float(cadence_values[i])})

        if i > 0 and i < len(distance_values):
            dist_curr = distance_values[i] or 0
            dist_prev = distance_values[i - 1] or 0
            if dist_curr > dist_prev:
                dist_delta_km = (dist_curr - dist_prev) / 1000.0
                time_delta_sec = (time_objs[i] - time_objs[i - 1]).total_seconds()
                if time_delta_sec > 0 and dist_delta_km > 0:
                    pace_min_per_km = (time_delta_sec / 60.0) / dist_delta_km
                    pace_dps.append({"timestamp": ts_ms, "value": pace_min_per_km})

    return hr_dps, pace_dps, cadence_dps


def _fetch_activities_chunked(
    api: Any,
    start_date: datetime,
    end_date: datetime,
    chunk_days: int,
) -> list[dict]:
    """
    Fetch activities for [start_date, end_date] inclusive, using non-overlapping chunks.
    Deduplicates by activityId.
    """
    if start_date > end_date:
        return []

    seen_ids: set[str] = set()
    merged: list[dict] = []

    cursor = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end_date.replace(hour=0, minute=0, second=0, microsecond=0)

    while cursor <= end_day:
        chunk_end = min(end_day, cursor + timedelta(days=chunk_days - 1))
        start_str = cursor.strftime("%Y-%m-%d")
        end_str = chunk_end.strftime("%Y-%m-%d")
        try:
            chunk = api.get_activities_by_date(start_str, end_str) or []
        except Exception as e:
            print(f"Garmin API error for {start_str}..{end_str}: {e}")
            chunk = []

        for act in chunk:
            aid = str(act.get("activityId", act.get("activityUUID", "")))
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                merged.append(act)

        cursor = chunk_end + timedelta(days=1)

    return merged


def _normalize_garmin_token_blob(raw: str) -> str:
    """
    Garth session tokens are one base64 line. Common breaks in CDF / .env:
    - Line wraps in .env or Fusion (must be a single line)
    - Accidental `GARMIN_TOKENS=` prefix pasted into the value
    - UTF-8 BOM from Notepad / Excel
    - Spaces where base64 had '+' (URL/query mangling)
    """
    if not raw:
        return ""
    s = raw.strip()
    if s.startswith("\ufeff"):
        s = s.lstrip("\ufeff")
    head = s[:48].lower()
    if "garmintokens=" in head or "garmin_tokens=" in head:
        if "=" in s:
            s = s.split("=", 1)[1].strip()
    # Remove all whitespace (including newlines from wrapped .env values)
    s = re.sub(r"\s+", "", s)
    s = s.replace(" ", "+")
    return s


def _validate_garth_token_blob(s: str) -> tuple[bool, str | None]:
    """
    Check base64 → UTF-8 JSON before garth.loads, for clearer errors when the
    blob is truncated or corrupted (otherwise: UnicodeDecodeError at byte 0xa2…).
    """
    if not s:
        return False, "empty token"
    pad = (-len(s)) % 4
    try:
        raw = base64.b64decode(s + ("=" * pad), validate=False)
    except binascii.Error as e:
        return False, f"invalid base64: {e}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return (
            False,
            (
                f"token is not valid UTF-8 JSON after base64 decode ({e}). "
                "Usually: line-wrapped .env, truncated value, or bad copy-paste. "
                "Re-export with `poetry run python scripts/export_garmin_tokens.py`, "
                "keep GARMIN_TOKENS on one line, or store the blob in Cognite secret `garmin-tokens`."
            ),
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return False, f"token JSON parse failed: {e}"
    if not isinstance(data, list) or len(data) != 2:
        return False, "token must be a JSON array of two objects (Garth oauth1 + oauth2)"
    return True, None


def handle(
    client: CogniteClient,
    secrets: dict,
    data: dict | None = None,
) -> dict:
    """
    Main entry point. Needs GARMINTOKENS env and/or token/password secrets (set in Fusion after deploy).

    data:
      days_back: int — look back this many days from today (default 7).
      chunk_days: int — Garmin API window size in days (default: 30 if days_back > 90, else one call).
      include_tcx: bool — download TCX for time series (default True; auto False if days_back > 90 unless set).
      space_name: str — data model space (default garmin_coach_space).
    """
    report_extraction_pipeline(client, "seen")

    email = (secrets or {}).get("garmin-email") or (secrets or {}).get("garmin_email") or ""
    password = (secrets or {}).get("garmin-password") or (secrets or {}).get("garmin_password") or ""
    token_secret = _normalize_garmin_token_blob(
        (secrets or {}).get("garmin-tokens") or (secrets or {}).get("garmin_tokens") or ""
    )
    env_tokens = _normalize_garmin_token_blob(os.environ.get("GARMINTOKENS") or "")
    # Prefer Cognite secret (full blob, no env length / YAML issues)
    token_blob = token_secret if token_secret else env_tokens

    if not token_blob and (not email or not password):
        report_extraction_pipeline(
            client,
            "failure",
            "Need GARMINTOKENS env (session blob) or garmin-email + garmin-password secrets",
        )
        return {
            "error": (
                "Missing Garmin auth: set function env GARMINTOKENS (see module README) "
                "or secrets garmin-email and garmin-password"
            ),
        }

    data = data or {}
    days_back = int(data.get("days_back", DEFAULT_DAYS_BACK))
    space_name = data.get("space_name", "garmin_coach_space")

    # include_tcx: explicit wins; else disable for large backfills to avoid function timeouts
    if "include_tcx" in data:
        include_tcx = bool(data["include_tcx"])
    else:
        include_tcx = days_back <= LARGE_BACKFILL_THRESHOLD_DAYS

    # chunk_days: explicit wins; else chunk large ranges
    if "chunk_days" in data and data["chunk_days"] is not None:
        chunk_days = max(1, int(data["chunk_days"]))
    elif days_back > LARGE_BACKFILL_THRESHOLD_DAYS:
        chunk_days = DEFAULT_CHUNK_DAYS_LARGE
    else:
        chunk_days = max(days_back, 1)

    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days_back)

    try:
        from garminconnect import Garmin

        # Token blob (>512 chars): garth OAuth1+2 session; avoids fresh password login in serverless.
        if token_blob and len(token_blob) > 512:
            ok, v_err = _validate_garth_token_blob(token_blob)
            if not ok:
                report_extraction_pipeline(client, "failure", v_err or "invalid token")
                return {
                    "error": (
                        f"Invalid GARMIN_TOKENS / garmin-tokens session blob: {v_err}. "
                        "See garmin_coach README."
                    ),
                }
            api = Garmin()
            api.login(tokenstore=token_blob)
        else:
            # Password path (may fail with MFA / "OAuth1 token required for OAuth2 refresh" — use GARMINTOKENS)
            api = Garmin(email, password)
            api.login()
    except Exception as e:
        report_extraction_pipeline(client, "failure", str(e))
        err = str(e)
        hint = ""
        if "OAuth1 token" in err or "OAuth2 refresh" in err:
            hint = (
                " Export a session token: run locally `poetry run python scripts/export_garmin_tokens.py` "
                "(see garmin_coach README), add GARMIN_TOKENS to .env, cdf deploy."
            )
        elif "utf-8" in err.lower() or "codec can't decode" in err.lower():
            hint = (
                " Token blob may be line-wrapped, truncated, or pasted twice. "
                "Re-export one line; or set Cognite secret `garmin-tokens` (full blob) instead of env only."
            )
        return {"error": f"Garmin login failed: {e}{hint}"}

    activities = _fetch_activities_chunked(api, start_date, end_date, chunk_days)

    if not activities:
        report_extraction_pipeline(client, "success")
        return {
            "ingested": 0,
            "message": "No activities in date range",
            "days_back": days_back,
            "chunk_days": chunk_days,
            "include_tcx": include_tcx,
        }

    events_to_create = []
    nodes_to_apply = []
    ts_datapoints: dict[str, list[dict]] = {}

    for act in activities:
        summary = extract_summary(act)
        if not summary["activity_id"] or not summary["start_time"]:
            continue

        ext_id = f"{EXTERNAL_ID_PREFIX}{summary['activity_id']}"

        end_time_ms = summary["start_time"] + int((summary["duration_seconds"] or 0) * 1000)
        events_to_create.append(
            EventWrite(
                external_id=ext_id,
                start_time=summary["start_time"],
                end_time=end_time_ms,
                metadata={
                    "activity_type": summary["activity_type"],
                    "activity_name": summary["activity_name"] or "",
                    "distance_km": str(summary["distance_km"]) if summary["distance_km"] is not None else "",
                    "avg_heart_rate": str(summary["avg_heart_rate"]) if summary["avg_heart_rate"] is not None else "",
                    "duration_seconds": str(summary["duration_seconds"]) if summary["duration_seconds"] else "",
                },
            )
        )

        props = {k: v for k, v in summary.items() if v is not None}
        nodes_to_apply.append(
            NodeApply(
                space=space_name,
                external_id=ext_id,
                sources=[
                    {
                        "source": {
                            "space": space_name,
                            "externalId": "Workout",
                            "version": "1.0",
                            "type": "view",
                        },
                        "properties": props,
                    }
                ],
            )
        )

        if include_tcx:
            activity_id = summary["activity_id"]
            try:
                tcx_bytes = api.download_activity(activity_id, dl_fmt=api.ActivityDownloadFormat.TCX)
                if tcx_bytes:
                    hr_dps, pace_dps, cadence_dps = parse_tcx_to_timeseries(tcx_bytes, activity_id)
                    if hr_dps:
                        ts_id = f"garmin_hr_{activity_id}"
                        ts_datapoints[ts_id] = hr_dps
                    if pace_dps:
                        ts_id = f"garmin_pace_{activity_id}"
                        ts_datapoints[ts_id] = pace_dps
                    if cadence_dps:
                        ts_id = f"garmin_cadence_{activity_id}"
                        ts_datapoints[ts_id] = cadence_dps
            except Exception as e:
                print(f"TCX download/parse failed for {activity_id}: {e}")

    try:
        if events_to_create:
            # Idempotent: safe to re-run overlapping daily sync
            client.events.upsert(events_to_create)

        if nodes_to_apply:
            client.data_modeling.instances.apply(nodes=nodes_to_apply)

        if ts_datapoints:
            from cognite.client.data_classes import TimeSeries
            from cognite.client.exceptions import CogniteNotFoundError

            for ts_id, dps in ts_datapoints.items():
                dps_tuples = [(d["timestamp"], d["value"]) for d in dps]
                try:
                    client.time_series.retrieve(external_id=ts_id)
                except CogniteNotFoundError:
                    client.time_series.create(
                        TimeSeries(
                            external_id=ts_id,
                            name=f"Garmin {ts_id.split('_')[1]} - {ts_id.split('_')[-1]}",
                        )
                    )
                client.time_series.data.insert(datapoints=dps_tuples, external_id=ts_id)
    except Exception as e:
        report_extraction_pipeline(client, "failure", str(e))
        raise

    report_extraction_pipeline(client, "success")
    return {
        "ingested": len(events_to_create),
        "events": len(events_to_create),
        "workouts": len(nodes_to_apply),
        "time_series": len(ts_datapoints),
        "days_back": days_back,
        "chunk_days": chunk_days,
        "include_tcx": include_tcx,
        "activities_fetched": len(activities),
    }
