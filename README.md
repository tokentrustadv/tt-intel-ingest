# TT Intel Ingest

Replaces the Colab notebook (`ALEN_Sessions_Insert_v2.ipynb`) in the ALEN Sessions
pipeline with a single local script. No Google Drive step, no notebook.

**Ritual:** export a CSV from Chatbase by hand (unchanged) → `python ingest.py path/to/export.csv`.

## What it does

1. Reads the CSV you pass on the command line.
2. Parses each session block and enriches it (asset detection, OTE layer mapping,
   routing outcome, session depth, etc.) using the same logic as the old notebook.
3. Upserts each session into Supabase's `alen_sessions` table, keyed on
   `conversation_id`. Re-running on the same file, or on a new export that
   overlaps a previous one, never creates duplicates.
4. Prints a summary: rows read, inserted, updated, skipped.

The CSV is trusted as-is — Chatbase's export filters have already excluded
unfinished chats and test sessions before the file reaches this script. The
only rows this script skips are structurally broken ones (missing a
conversation ID, missing timestamps, no messages) — not a re-derivation of
"unfinished" or "test" rules.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in `SUPABASE_SERVICE_KEY` (Project Settings → API →
service_role secret for the `token-trust-intel` project). `SUPABASE_URL` is
already filled in.

## Usage

Dry run first — parses and prints what would be written, touches nothing in
Supabase:

```bash
python ingest.py path/to/export.csv --dry-run
```

Real run:

```bash
python ingest.py path/to/export.csv
```

## Verification (do these before trusting it)

1. **Dry run** on a real export. Compare a couple of the printed rows
   (conversation ID, assets, routed_to) against sessions already in
   `alen_sessions` to confirm the mapping still lines up.
2. **Real run** on that same export. Confirm the row counts match what you
   expect and no duplicates show up in the table.
3. **Re-run the exact same CSV.** Every row should come back as "updated",
   never as a new insert — that's the idempotency check.

## Notes

- Never destructive: only inserts/updates via upsert on `conversation_id`.
  No `DELETE`, no `TRUNCATE`, no schema changes.
- Fails loudly: a malformed CSV (no valid sessions) or a Supabase error exits
  non-zero with a message and writes nothing partial — all sessions from one
  run are upserted in a single batched call.
- No filtering beyond skipping structurally broken rows — see "What it does"
  above.
- No Chatbase API integration, no scheduling. This is a manual, twice-a-month
  command.
