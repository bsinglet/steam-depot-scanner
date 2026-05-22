"""
query_results.py
================
Query tool for the all_files.db produced by steam_depot_scanner.py.

Filters can be stacked freely - all supplied filters are ANDed together.

Usage:
    python query_results.py                                   # overall summary
    python query_results.py --game "half life"                # fuzzy game name search
    python query_results.py --game portal --filter .pdb       # stacked filters
    python query_results.py --filter assembly-csharp.dll      # all Unity games
    python query_results.py --app 730                         # by exact App ID
    python query_results.py --search symbols                  # substring in file path
    python query_results.py --stats                           # files-per-game leaderboard
    python query_results.py --list-games                      # show all known game names
    python query_results.py --game portal --csv out.csv       # export to CSV

Fuzzy matching notes:
    --game splits your query into words and scores each game name by how many
    of those words appear in it (punctuation and case ignored).  All games that
    match at least one word are shown, ranked best-match first.  You can also
    use --game with --min-score to tighten the threshold (default: 1).

    Examples:
        --game "garry mod"        matches "Garry's Mod"
        --game "half life 2"      matches "Half-Life 2", "Half-Life 2: Episode One", etc.
        --game "cs"               matches any name containing "cs"
"""

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("results") / "all_files.db"


# ─────────────────────────── DB ──────────────────────────────────────────────

def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"Database not found at {DB_PATH}. Run steam_depot_scanner.py first.")
    return sqlite3.connect(DB_PATH)


# ─────────────────────── FUZZY GAME MATCHING ─────────────────────────────────

def _tokenise(text: str) -> list[str]:
    """Lower-case, strip punctuation, split on whitespace."""
    return re.sub(r"[^a-z0-9 ]", " ", text.lower()).split()


def _acronym(app_name: str) -> str:
    """First letter of each word after splitting on non-alphanumeric chars."""
    words = re.sub(r"[^a-z0-9 ]", " ", app_name.lower()).split()
    return "".join(w[0] for w in words if w)


def score_name(query_tokens: list[str], app_name: str) -> int:
    """
    Return how many query tokens appear anywhere in the app_name.
    Each token is matched against three forms of the name:
      - punctuation replaced by spaces  ("Counter-Strike" -> "counter strike")
      - punctuation fully removed       ("Counter-Strike" -> "counterstrike")
      - acronym of words                ("Counter-Strike" -> "cs")
    This means 'cs' matches 'Counter-Strike', 'half' matches 'Half-Life', etc.
    A single-token query that exactly equals the acronym counts as a full match.
    """
    lower    = app_name.lower()
    spaced   = re.sub(r"[^a-z0-9 ]", " ", lower)
    squished = re.sub(r"[^a-z0-9]",  "",  lower)
    acronym  = _acronym(app_name)
    return sum(
        1 for t in query_tokens
        if t in spaced or t in squished or acronym.startswith(t)
    )


def fuzzy_match_app_ids(con: sqlite3.Connection,
                        query: str,
                        min_score: int = 1) -> tuple[list[int], dict[int, str]]:
    """
    Return (ordered_app_ids, {app_id: app_name}) for games whose names
    match the query, sorted best-match first then alphabetically.
    """
    rows = con.execute(
        "SELECT DISTINCT app_id, app_name FROM depot_files ORDER BY app_name"
    ).fetchall()

    tokens = _tokenise(query)
    if not tokens:
        return [], {}

    scored = []
    for app_id, app_name in rows:
        s = score_name(tokens, app_name or "")
        if s >= min_score:
            scored.append((s, app_name or "", app_id))

    # Sort: highest score first, then alphabetical name
    scored.sort(key=lambda x: (-x[0], x[1]))

    ordered_ids  = [app_id  for _, _, app_id  in scored]
    id_to_name   = {app_id: name for _, name, app_id in scored}
    return ordered_ids, id_to_name


# ─────────────────────── DISPLAY HELPERS ─────────────────────────────────────

def print_rows(rows: list, headers: list[str]):
    if not rows:
        print("  (no results)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = min(max(widths[i], len(str(cell or ""))), 80)
    fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)
    sep = "  " + "  ".join("-" * w for w in widths)
    print(fmt.format(*headers))
    print(sep)
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            s = str(cell or "")
            cells.append(s if len(s) <= widths[i] else s[:widths[i]-1] + "…")
        print(fmt.format(*cells))
    print(f"\n  {len(rows):,} row(s)")


def export_csv(rows: list, headers: list[str], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    print(f"Exported {len(rows):,} rows to {path}")


# ─────────────────────── QUERY BUILDER ───────────────────────────────────────

def build_query(app_ids: list[int] | None,
                file_filter: str | None,
                path_search: str | None) -> tuple[str, list]:
    """
    Build a SELECT statement with optional WHERE clauses.
    Returns (sql, params).
    """
    conditions = []
    params     = []

    if app_ids is not None:
        placeholders = ",".join("?" * len(app_ids))
        conditions.append(f"app_id IN ({placeholders})")
        params.extend(app_ids)

    if file_filter:
        pat = file_filter.lower()
        conditions.append(
            "(LOWER(file_path) LIKE ? OR "
            " LOWER(SUBSTR(file_path, LENGTH(file_path) "
            "   - LENGTH(REPLACE(file_path, '/', '')) + 1 + 1)) = ?)"
        )
        params += [f"%{pat}", pat]

    if path_search:
        conditions.append("LOWER(file_path) LIKE ?")
        params.append(f"%{path_search.lower()}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT app_id, app_name, depot_id, file_path
        FROM depot_files
        {where}
        ORDER BY app_name, depot_id, file_path
    """
    return sql, params


# ──────────────────────────── MAIN ───────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query steam_depot_scanner results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage:")[1] if "Usage:" in __doc__ else "",
    )
    parser.add_argument("--game",      help="Fuzzy game name search (partial words ok)")
    parser.add_argument("--filter",    help="File suffix or exact filename (e.g. .pdb)")
    parser.add_argument("--search",    help="Substring to match anywhere in the file path")
    parser.add_argument("--app",       type=int, help="Exact App ID to filter by")
    parser.add_argument("--stats",     action="store_true",
                        help="Show per-game file counts (leaderboard)")
    parser.add_argument("--list-games", action="store_true",
                        help="List all game names in the database")
    parser.add_argument("--min-score", type=int, default=1, metavar="N",
                        help="Minimum fuzzy match score for --game (default: 1)")
    parser.add_argument("--csv",       help="Export results to this CSV file")
    args = parser.parse_args()

    con     = connect()
    headers = ["App ID", "App Name", "Depot ID", "File Path"]

    # ── --list-games ──────────────────────────────────────────────────────────
    if args.list_games:
        rows = con.execute("""
            SELECT app_id, app_name, COUNT(*) as files
            FROM depot_files
            GROUP BY app_id
            ORDER BY app_name
        """).fetchall()
        print_rows(rows, ["App ID", "App Name", "File Count"])
        con.close()
        return

    # ── --stats ───────────────────────────────────────────────────────────────
    if args.stats:
        rows = con.execute("""
            SELECT app_id, app_name, COUNT(*) AS file_count
            FROM depot_files
            GROUP BY app_id
            ORDER BY file_count DESC
        """).fetchall()
        print_rows(rows, ["App ID", "App Name", "File Count"])
        if args.csv:
            export_csv(rows, ["App ID", "App Name", "File Count"], args.csv)
        con.close()
        return

    # ── Resolve app IDs from --game or --app ──────────────────────────────────
    app_ids   = None
    name_note = ""

    if args.game:
        matched_ids, id_to_name = fuzzy_match_app_ids(con, args.game, args.min_score)
        if not matched_ids:
            print(f"No games found matching '{args.game}'.")
            print("Tip: try fewer or shorter words, or run --list-games to browse all names.")
            con.close()
            return
        app_ids   = matched_ids
        name_note = f" in {len(matched_ids)} game(s) matching '{args.game}'"
        # Show which games were matched
        print(f"Matched {len(matched_ids)} game(s):")
        for aid in matched_ids:
            print(f"  [{aid}] {id_to_name[aid]}")
        print()

    if args.app:
        # --app overrides or intersects with --game
        if app_ids is not None:
            app_ids = [a for a in app_ids if a == args.app]
        else:
            app_ids = [args.app]

    # ── Build and run query ───────────────────────────────────────────────────
    any_filter = args.game or args.app or args.filter or args.search

    if not any_filter:
        # Default: print summary
        total  = con.execute("SELECT COUNT(*)                FROM depot_files").fetchone()[0]
        apps   = con.execute("SELECT COUNT(DISTINCT app_id)  FROM depot_files").fetchone()[0]
        depots = con.execute("SELECT COUNT(DISTINCT depot_id) FROM depot_files").fetchone()[0]
        print("Database summary:")
        print(f"  Total file entries : {total:,}")
        print(f"  Distinct apps      : {apps:,}")
        print(f"  Distinct depots    : {depots:,}")
        print("\nOptions: --game  --filter  --search  --app  --stats  --list-games  --csv")
        print("         (--help for full usage)")
        con.close()
        return

    sql, params = build_query(
        app_ids     = app_ids,
        file_filter = args.filter,
        path_search = args.search,
    )
    rows = con.execute(sql, params).fetchall()

    # Describe what we searched for
    desc_parts = []
    if args.game:   desc_parts.append(f"game~'{args.game}'")
    if args.app:    desc_parts.append(f"appid={args.app}")
    if args.filter: desc_parts.append(f"file='{args.filter}'")
    if args.search: desc_parts.append(f"path~'{args.search}'")
    print(f"Results for: {' AND '.join(desc_parts)}")

    print_rows(rows, headers)

    if args.csv:
        export_csv(rows, headers, args.csv)

    con.close()


if __name__ == "__main__":
    main()
