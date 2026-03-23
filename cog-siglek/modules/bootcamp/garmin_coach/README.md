# Garmin Coach Module

Ingests Garmin Connect activities into CDF and enables AI-powered coaching via cognite-ai or Atlas AI.

## Sync behavior

| Mode | When | Parameters |
|------|------|------------|
| **Daily schedule** | 02:00 UTC | Last **2 days**, `include_tcx: true`, `chunk_days: 2` — incremental sync with HR/pace/cadence time series |
| **Initial / historical load** | Run once manually | **~3 years** = `days_back: 1095`, `chunk_days: 30`, **`include_tcx: false`** recommended (faster, avoids function timeouts) |

Large lookbacks (`days_back` > 90) automatically use **30-day API chunks** and default **`include_tcx` to false** unless you set `include_tcx: true` explicitly. Events and Workout rows are **upserted** by `external_id`, so overlapping daily runs are safe.

## Setup

### 1. Garmin authentication (read this — CDF / serverless)

Password-only login often fails in the cloud with:

`Login failed: OAuth1 token is required for OAuth2 refresh`

That comes from the **Garth** library used by `garminconnect` on a **fresh login** (no saved token file). Cognite Functions also have **no home directory** for `~/.garminconnect`, so every run is a “fresh” login.

**Recommended:** use a **session token blob** in **`GARMIN_TOKENS`** (mapped to function env **`GARMINTOKENS`**). The handler uses it first when it is longer than 512 characters.

1. On your **laptop** (same repo, Poetry env):

   ```bash
   poetry update garminconnect garth
   poetry run python scripts/export_garmin_tokens.py
   ```

   The script uses **`garth.login()` + `dumps()`** (not `Garmin().login()`), because the latter can fail with `OAuth1 token is required for OAuth2 refresh` after a successful SSO when it loads profile — same error you may see in Fusion. If the script still fails, try **`uvx garth login`** and use the printed token line the same way.

2. Run **`cdf build`** and **`cdf deploy`** (Garmin auth is not in [`functions.Function.yaml`](functions/functions.Function.yaml) — CDF rejects null `${GARMIN_*}` at deploy if those env vars are missing).

3. In **Fusion** → your function → **Environment variables** / **Secrets**: add **`GARMINTOKENS`** (paste the long token value) and/or secrets **`garmin-tokens`** (same blob; preferred for very long strings), **`garmin-email`**, **`garmin-password`**. Cognite secret **names** must use only `a-z`, `0-9`, and `-`.

4. Re-run the function in Fusion. **When Garmin expires the session**, export again and update Fusion.

**If you see** `utf-8 codec can't decode byte ... invalid start byte`: the session string is **corrupted** (line-wrapped value, truncation, bad copy). Paste the blob on **one line** in Fusion.

**Optional:** you can instead put **`GARMIN_TOKENS`** in **`.env`** for local deploys only if you add secrets/env back into the function YAML with real values — not recommended for shared repos because empty env breaks `cdf deploy`.

### 2. Schedule authentication (required for the schedule to run)

[`schedules.Schedule.yaml`](functions/schedules.Schedule.yaml) uses **`function_schedule_client_id`** / **`function_schedule_client_secret`** from [`config.dev.yaml`](../../../config.dev.yaml) (same app registration as the Ice Cream function schedules: **`ICAPI_EXTRACTORS_CLIENT_ID`** and **`ICAPI_EXTRACTORS_CLIENT_SECRET`**). Set those in `.env` or GitHub **vars/secrets** so Cognite can invoke the scheduled run without falling back to toolkit credentials.

To use a **dedicated** app registration instead, change those two entries under `modules.bootcamp.garmin_coach` to point at your own env vars.

### 3. One-time ~3 year backfill (initial load)

Run **once** after deploy (Fusion **Run function**, or Python SDK). Use **without TCX** first to reduce runtime; you can backfill TCX later per activity if needed.

```python
client.functions.call(
    external_id="garmin_activities_extractor",
    data={
        "days_back": 1095,       # ~3 years
        "chunk_days": 30,        # Garmin API calls in 30-day windows
        "include_tcx": False,    # strongly recommended for multi-year
        "space_name": "garmin_coach_space",
    },
    client_credentials=...,  # or use interactive auth per your setup
)
```

If the run **times out**, run again with a smaller `days_back` (e.g. 365, then 730, then 1095) — upserts dedupe by activity id.

### 4. Daily schedule

No extra action beyond §2: the deployed schedule calls the same function with `days_back: 2` and `include_tcx: true`.

## AI chat

Use the Jupyter notebook at `rmdm_support/notebooks/garmin_coach_chat.ipynb` to query workout data with cognite-ai SmartDataframe.

## Data stored

- **Events**: Workout summaries (upsert by `garmin_activity_{id}`)
- **Data model**: Workout nodes in `garmin_coach_space`
- **Time series**: HR / pace / cadence from TCX when `include_tcx` is true
