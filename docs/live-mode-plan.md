# Live mode plan

The DJ today is a **demo**: weather and occupancy are manual keys and knobs. **Live mode** takes them from real data.
A DEMO / LIVE slide switch on the controller changes between the two.

Constraint: **no changes to O3's database.** Supabase is read-only from the DJ.

## Data sources

| Input | Live source | Notes |
|---|---|---|
| Time of day | the venue's clock | from `venues.<code>.timezone`; the time knob is disabled in live mode |
| Weather | Open-Meteo, fetched by the DJ every 10 min | free, no key; venue lat/lon from `venues` |
| Occupancy | O3 Supabase (`o3-flutter-app` production), read-only, every 5 min | see below |

Set **one line**, `live.venue` (e.g. `"sydney_01"`), and timezone, weather location and the Supabase venue code follow.

### What already exists in O3's Supabase (checked 26 Sep 2026, read-only)

- `physical_location`: the 5 venues. `location_code` is the key (`sydney_01` Newtown, `sydney_02` Sydney/Haymarket (inactive),
  `melbourne_01` Southbank, `melbourne_02` Brunswick, `auckland_01` Auckland/Newmarket), and it stores capacity:
  `green_capacity_threshold` / `red_capacity_threshold` (e.g. Newtown 120 / 160), which the scan-in portal uses to block scan-ins when full.
- `user_public_location`: one row per person currently scanned in (`user_id`, `location_name` holding the venue code, `created_at`).
  Row-level security hides it from the anonymous key (`USING (false)`) and limits signed-in users to people they're allowed to see,
  so counting rows directly would be wrong.
- **`get_location_occupancy_counts(p_location_codes text[])`** → `(location_code, occupancy)`: an existing *security definer* function
  that counts everyone at each venue and returns only numbers. Its only requirement is `auth.uid() is not null`, i.e. the caller is
  signed in as any O3 app user.

### How the DJ reads occupancy (`live.occupancy.mode = "o3"`, `o3dj/live.py`)

1. Signs in as an O3 app account with the app's public anon key (standard Supabase password sign-in, the same as the app;
   the session refreshes itself).
2. Calls `get_location_occupancy_counts` for the venue code every 5 minutes (`poll_seconds`). Flipping to live can force a
   refresh, but never within 60 s of the last request, and failures wait for the next scheduled poll (no retry loops).
3. Reads the venue's `red_capacity_threshold` as "100% full" every 6 hours, unless `live.capacity` is set.

Load on Supabase: about 13 requests an hour per DJ (12 occupancy calls, about 1 session refresh, a capacity read every 6 h).

No tables, functions, policies or users are created or changed. Requests are limited to: sign-in, that function, and reading
`physical_location` (tested in `tests/test_live.py`).

### Providing access

Put these in `config.local.json` (gitignored; see `config.local.example.json`) or as environment variables before starting the DJ:

| What | Where to get it | Env var |
|---|---|---|
| venue code | table above | (config only) `live.venue` |
| anon / publishable key | Supabase → o3-flutter-app → Project Settings → API Keys (public; the same key ships in the O3 app) | `O3_SUPABASE_KEY` |
| an O3 app account | ideally a dedicated one for the DJ (e.g. `dj-newtown@…`) made through the normal app sign-up | `O3_DJ_EMAIL`, `O3_DJ_PASSWORD` |

**Never** use the service-role key. It isn't needed.

## Behaviour

- **Layered inputs:** each value is a staff override if one is active, otherwise the live value, otherwise a neutral default. `brain.py` is unchanged.
- **Overrides:** in live mode, pressing a weather key or turning the occupancy knob overrides live data for `live.override_minutes` (60),
  then hands back. Shown on screen as `override · 42m`.
- **Stale data:** if a feed hasn't updated for `live.stale_after_minutes` (15), the DJ falls back to neutral (clear weather,
  occupancy ignored) and shows `stale`. The music never depends on the feeds.
- **Re-picks:** a live change in weather, or occupancy moving across the threshold, re-picks upcoming tracks like a manual change
  (never touching the next track; see CLAUDE.md).
- Genre, energy/volume trims, votes and skips stay manual in both modes.

## Phases

1. **Done:** mode switch, layered inputs with overrides and staleness, Open-Meteo weather, configurable read-only
   Supabase occupancy, screen badges, tests.
2. **Done:** found `get_location_occupancy_counts`; the O3 mode signs in and calls it. **Remaining:** venue + key + DJ account (above).
3. **Pilot:** a week in live mode at one venue; compare the DJ log's reasons against how the room felt.

## Still to decide

1. Which venue these speakers are in (`live.venue`).
2. Which O3 account the DJ signs in with (a dedicated one is best).
3. Override length (default 60 min).
