# Live mode plan

The DJ today is a **demo**: weather and occupancy are manual keys and knobs. **Live mode** takes them from real data.
A DEMO / LIVE key switches between the two.

Constraint: **no changes to O3's database.** Supabase is read-only from the DJ.

## Data sources

| Input | Live source | Notes |
|---|---|---|
| Time of day | the venue's clock (`live.timezone`) | the time knob is disabled in live mode |
| Weather | Open-Meteo, fetched by the DJ every 10 min (`live.weather`) | free, no key; venue lat/lon in config |
| Occupancy | Supabase, read-only, every 60 s (`live.occupancy`) | people currently in the space ÷ `live.capacity` |

Occupancy can be read two ways, both read-only (see `o3dj/live.py`):

- **`count`**: counts rows in an existing table via PostgREST (e.g. visits at this site that started in the last few hours
  and have no end time). Uses a `HEAD` request with `Prefer: count=exact`, so no rows or personal data are transferred.
- **`rpc`**: calls an existing function that already returns a live headcount (preferred if the O3 app has one).

The key goes in `config.local.json` or the `O3_SUPABASE_KEY` env var, never in git. Use a key or login that existing
row-level security allows to read that table. **Don't use the service-role key**: it bypasses all access rules and would sit on a
laptop in a public cafe. Caveat: if the key can't see the rows, RLS makes the count come back as 0 rather than an error. Check it
against a busy moment.

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

1. **Done in this change:** mode switch, layered inputs with overrides and staleness, Open-Meteo weather, configurable read-only
   Supabase occupancy, screen badges, tests.
2. **Configure occupancy:** find where check-ins live (or an existing live-count function) and fill in `live.occupancy`.
   Needs the O3 app repo or someone who knows the schema.
3. **Pilot:** a week in live mode at one venue; compare the DJ log's reasons against how the room felt.

## Still to decide

1. Which venue these speakers are in (sets `timezone`, `lat`/`lon`, `capacity`, `site_value`).
2. Which table/function holds live check-ins, whether there's a check-out time, and what key can read it.
3. Override length (default 60 min).
