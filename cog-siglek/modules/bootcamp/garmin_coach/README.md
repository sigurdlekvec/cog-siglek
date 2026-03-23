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

2. Copy the printed **`GARMIN_TOKENS=...`** line into **`.env`** (single long line, do not commit).

3. **`cdf build`** and **`cdf deploy`** so the function gets the env var.

4. Re-run the function in Fusion. **When Garmin expires the session**, export again and redeploy.

**If you see** `utf-8 codec can't decode byte ... invalid start byte`: the session string reached CDF **corrupted** (almost always **line-wrapped** `.env`, **truncated** value, or a bad copy). Fix: put **`GARMIN_TOKENS=` on one line** in `.env` (no line breaks inside the value), re-export, redeploy. Deploy also maps the same value to Cognite secret **`garmin-tokens`** — the function **prefers** that secret over the env var, which avoids some tooling limits on long env values.

**Optional fallback:** email/password secrets — set **`GARMIN_EMAIL`** and **`GARMIN_PASSWORD`** in `.env` and deploy. This may still fail if Garmin requires MFA or hits the OAuth error above.

Secrets are declared in [`functions.Function.yaml`](functions/functions.Function.yaml). Cognite **secret key names** must use only `a-z`, `0-9`, and `-` (e.g. `garmin-email`, `garmin-password`, `garmin-tokens`). Env var **`GARMINTOKENS`** is the name Garth reads inside `login()`; we set it from **`${GARMIN_TOKENS}`** at deploy.

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
