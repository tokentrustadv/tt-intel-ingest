#!/usr/bin/env python3
"""
Ingest a Chatbase CSV export of ALEN sessions into Supabase's alen_sessions table.

Usage:
    python ingest.py path/to/export.csv
    python ingest.py path/to/export.csv --dry-run

Parsing and enrichment logic ported from ALEN_Sessions_Insert_v2.ipynb.
The CSV is trusted as-is (Chatbase's export filters already excluded
unfinished/test sessions) — this script does not re-filter sessions,
it only skips rows that are structurally broken (no conversation ID,
no timestamps, no messages).
"""
import argparse
import os
import re
import sys

TABLE_NAME = "alen_sessions"

# ── Asset -> OTE layer mapping (from ALEN_Sessions_Insert_v2.ipynb) ─────────
ASSET_LAYER_MAP = {
    # Capital
    'bitcoin': 'Capital', 'btc': 'Capital',
    # Settlement
    'xrp': 'Settlement', 'stellar': 'Settlement', 'xlm': 'Settlement',
    'clearpool': 'Settlement', 'cpool': 'Settlement',
    # Execution
    'eth': 'Execution', 'ethereum': 'Execution',
    'solana': 'Execution', 'sol': 'Execution',
    'avax': 'Execution', 'avalanche': 'Execution',
    'uni': 'Execution', 'uniswap': 'Execution',
    'aave': 'Execution', 'mkr': 'Execution', 'maker': 'Execution',
    'sky': 'Execution', 'crv': 'Execution', 'curve': 'Execution',
    'euler': 'Execution', 'eul': 'Execution',
    'zebec': 'Execution',
    # Interoperability
    'atom': 'Interoperability', 'dot': 'Interoperability',
    'polkadot': 'Interoperability', 'matic': 'Interoperability',
    'polygon': 'Interoperability', 'arb': 'Interoperability',
    'arbitrum': 'Interoperability', 'op': 'Interoperability',
    'optimism': 'Interoperability',
    # Compute
    'render': 'Compute', 'rndr': 'Compute',
    'fil': 'Compute', 'filecoin': 'Compute',
    'ar': 'Compute', 'arweave': 'Compute',
    'icp': 'Compute',
    # Verifiability
    'mina': 'Verifiability', 'chainlink': 'Verifiability',
    'link': 'Verifiability', 'hbar': 'Verifiability',
    'hedera': 'Verifiability', 'pyth': 'Verifiability',
    # Agent Economy
    'reppo': 'Agent Economy',
    # DePIN
    'hnt': 'DePIN',
    # Canton / institutional
    'cc': 'Settlement', 'canton': 'Settlement',
    # Activity (noise layer)
    'sui': 'Activity', 'aptos': 'Activity', 'apt': 'Activity',
    'near': 'Activity', 'algo': 'Activity', 'algorand': 'Activity',
    'ada': 'Activity', 'cardano': 'Activity',
    'ltc': 'Activity', 'litecoin': 'Activity', 'bnb': 'Activity',
    'jup': 'Activity', 'jupiter': 'Activity',
    'wif': 'Activity', 'bonk': 'Activity', 'pepe': 'Activity',
    'doge': 'Activity', 'shib': 'Activity',
    'bat': 'Activity',
    # RWA / Financial Infra
    'ondo': 'Settlement', 'snx': 'Execution', 'synthetix': 'Execution',
    'ldo': 'Execution', 'lido': 'Execution', 'rpl': 'Execution',
    'comp': 'Execution',
    'sei': 'Execution', 'tia': 'Compute', 'celestia': 'Compute',
    'inj': 'Execution', 'injective': 'Execution',
}

ASSET_KEYWORDS = list(ASSET_LAYER_MAP.keys()) + ['layer 1', 'l1']

# Word-boundary patterns so short tickers (AR, OP, CC…) don't false-match
# inside words like "share", "stop", "account".
_ASSET_PATTERNS = {
    k: re.compile(r'(?<![a-z0-9])' + re.escape(k) + r'(?![a-z0-9])')
    for k in ASSET_KEYWORDS
}

ROUTING_KEYWORDS = [
    'tokentrust.substack.com', 'tokentrustadvisors.xyz',
    'subscribe', '/ttn', 'signals', 'token trust network',
]

_SEPARATOR = re.compile(r'_{10,}(?:,_{10,})+')
_CONV_RE = re.compile(
    r',([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
    r',([^,\n]+)'
)
_DATE_RE = re.compile(
    r',(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})'
)
_MSG_SECTION_RE = re.compile(r'Messages:,Role,Message\n(.*?)$', re.DOTALL)
_MSG_RE = re.compile(
    r',(assistant|user),"((?:[^"\\]|\\.)*?)"(?=\n,(?:assistant|user)|$)',
    re.DOTALL
)


# ── Parsing ──────────────────────────────────────────────────────────────
def parse_block(block):
    """Parse one session block. Returns (session_dict, None) or (None, skip_reason)."""
    block = block.strip()
    if not block:
        return None, "empty block"

    conv_match = _CONV_RE.search(block)
    if not conv_match:
        return None, "no conversation ID found"

    date_match = _DATE_RE.search(block)
    if not date_match:
        return None, "no created/last-message timestamps found"

    messages = []
    msg_section = _MSG_SECTION_RE.search(block)
    if msg_section:
        for m in _MSG_RE.finditer(msg_section.group(1)):
            messages.append({"role": m.group(1), "content": m.group(2).replace('""', '"')})
    if not messages:
        return None, "no messages found"

    return {
        "conversation_id": conv_match.group(1),
        "created_at": date_match.group(1).replace(" ", "T"),
        "last_message_at": date_match.group(2).replace(" ", "T"),
        "messages": messages,
    }, None


def count_user_exchanges(messages):
    return sum(1 for m in messages if m["role"] == "user")


def get_assets(messages):
    text = " ".join(m["content"] for m in messages if m["role"] == "user").lower()
    return list({k.upper() for k, pat in _ASSET_PATTERNS.items() if pat.search(text)})


def get_ote_layers(assets):
    return list({ASSET_LAYER_MAP[a.lower()] for a in assets if a.lower() in ASSET_LAYER_MAP})


def get_routed_to(messages):
    text = " ".join(m["content"] for m in messages if m["role"] == "assistant").lower()
    if "tokentrustadvisors.xyz/ttn" in text:
        return "TTN"
    elif "tokentrust.substack.com" in text:
        return "Signals/TTN"
    return "Signals"


def get_session_depth(exchanges):
    if exchanges <= 2:
        return "shallow"
    elif exchanges <= 4:
        return "medium"
    return "deep"


def get_primary_question(messages):
    for m in messages:
        if m["role"] == "user":
            return m["content"][:200]
    return None


def get_user_type(messages):
    text = " ".join(m["content"] for m in messages if m["role"] == "user").lower()
    advisor_signals = ['client', 'advisor', 'ria', 'family office', 'portfolio manager', 'my clients']
    if any(s in text for s in advisor_signals):
        return "advisor"
    return "investor"


def build_payload(session):
    msgs = session["messages"]
    assets = get_assets(msgs)
    exchanges = count_user_exchanges(msgs)
    return {
        "conversation_id": session["conversation_id"],
        "created_at": session["created_at"],
        "last_message_at": session["last_message_at"],
        "user_type": get_user_type(msgs),
        "assets_disclosed": assets,
        "ote_layers": get_ote_layers(assets),
        "primary_question": get_primary_question(msgs),
        "session_depth": get_session_depth(exchanges),
        "routed_to": get_routed_to(msgs),
        "raw_source": f"conversation_{session['conversation_id'][:8]}.csv",
    }


def parse_csv(raw_text):
    """Returns (payloads, skipped) where skipped is a list of (block_index, reason)."""
    blocks = _SEPARATOR.split(raw_text)
    payloads = []
    skipped = []
    for i, block in enumerate(blocks):
        session, reason = parse_block(block)
        if session is None:
            if reason != "empty block":
                skipped.append((i, reason))
            continue
        payloads.append(build_payload(session))
    return payloads, skipped


# ── Main ─────────────────────────────────────────────────────────────────
def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Ingest a Chatbase CSV export into alen_sessions.")
    parser.add_argument("csv_path", help="Path to the Chatbase CSV export")
    parser.add_argument("--dry-run", action="store_true", help="Parse and print, but don't write to Supabase")
    args = parser.parse_args()

    if not os.path.isfile(args.csv_path):
        fail(f"CSV file not found: {args.csv_path}")

    try:
        with open(args.csv_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        fail(f"Could not read CSV file: {e}")

    payloads, skipped = parse_csv(raw)
    total_read = len(payloads) + len(skipped)

    if not payloads:
        fail("No valid sessions parsed from this CSV — nothing to do. Check the file is a real Chatbase export.")

    for p in payloads:
        print(f"  {p['conversation_id'][:8]}...  {p['assets_disclosed']}  -> {p['routed_to']}")
    if skipped:
        print("\nSkipped (malformed):")
        for i, reason in skipped:
            print(f"  block {i}: {reason}")

    if args.dry_run:
        print(f"\n[DRY RUN] Read {total_read} rows: {len(payloads)} would be upserted, {len(skipped)} skipped (malformed).")
        print("[DRY RUN] No connection made to Supabase.")
        return

    load_dotenv_or_fail()
    try:
        from supabase import create_client
    except ImportError:
        fail("supabase package is not installed. Run: pip install -r requirements.txt")

    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not supabase_url or not supabase_key:
        fail("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")

    try:
        supabase = create_client(supabase_url, supabase_key)
    except Exception as e:
        fail(f"Could not create Supabase client: {e}")

    ids = [p["conversation_id"] for p in payloads]
    try:
        existing = supabase.table(TABLE_NAME).select("conversation_id").in_("conversation_id", ids).execute()
        existing_ids = {row["conversation_id"] for row in existing.data}
    except Exception as e:
        fail(f"Could not query existing sessions from Supabase: {e}")

    try:
        supabase.table(TABLE_NAME).upsert(payloads, on_conflict="conversation_id").execute()
    except Exception as e:
        fail(f"Supabase upsert failed — no rows were written: {e}")

    inserted = sum(1 for i in ids if i not in existing_ids)
    updated = sum(1 for i in ids if i in existing_ids)

    print(f"\nRead {total_read} rows: inserted {inserted}, updated {updated}, skipped {len(skipped)} (malformed).")


def load_dotenv_or_fail():
    try:
        from dotenv import load_dotenv
    except ImportError:
        fail("python-dotenv is not installed. Run: pip install -r requirements.txt")
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


if __name__ == "__main__":
    main()
