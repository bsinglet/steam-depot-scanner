"""
steam_depot_scanner.py
======================
Scans all owned Steam games for specific files in their depot manifests
WITHOUT downloading any game content.

Dependencies (only one non-stdlib package):
    pip install requests

Setup:
    1. Download DepotDownloader from https://github.com/SteamRE/DepotDownloader/releases
       Extract it and note the full path to DepotDownloader.exe
    2. Get a Steam Web API key from https://steamcommunity.com/dev/apikey
    3. Find your SteamID64 (shown at https://store.steampowered.com/account/ or steamid.io)
    4. Fill in the CONFIGURATION block below, then run:
           python steam_depot_scanner.py
    5. On the very first run, DepotDownloader will prompt for your password and
       Steam Guard code interactively in the terminal.  After that the login token
       is saved and all remaining apps run silently and unattended.

Outputs (written next to this script):
    manifests/          manifest_DEPOTID_MANIFESTID.txt files saved by DepotDownloader
    results/
        all_files.db    SQLite database - every file path in every depot scanned
        pdb_files.csv   Filtered: all .pdb files
        csharp_dlls.csv Filtered: all Assembly-CSharp.dll files
        report.txt      Human-readable summary report
"""

import os
import csv
import sys
import time
import argparse
import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

appdata = os.environ.get('APPDATA')


# ─────────────────────────── CONFIGURATION ───────────────────────────────────

STEAM_API_KEY    = ""   # https://steamcommunity.com/dev/apikey
STEAM_ID         = ""                  # e.g. "76561198012345678"
STEAM_USERNAME   = ""
DEPOT_DOWNLOADER = r"C:\Users\YourUserName\AppData\Local\Microsoft\WinGet\Packages\SteamRE.DepotDownloader_Microsoft.Winget.Source_8wekyb3d8bbwe\DepotDownloader.exe"  # appdata + r"\Local\Microsoft\WinGet\Packages\SteamRE.DepotDownloader_Microsoft.Winget.Source_8wekyb3d8bbwe\DepotDownloader.exe"

# Seconds to wait between apps - keeps things polite toward Steam's servers
DELAY_BETWEEN_APPS = 30.0

MANIFEST_DIR = Path("manifests")
RESULTS_DIR  = Path("results")
LOG_FILE     = Path("scanner.log")

# ─────────────────────────────────────────────────────────────────────────────

# Files to track.  Each entry: (match_type, pattern, output_csv_name)
#   "suffix" - file path ends with this string (case-insensitive)
#   "exact"  - bare filename exactly equals this (case-insensitive)
TARGETS = [
    ("suffix", ".pdb",                "pdb_files.csv"),
    ("exact",  "assembly-csharp.dll", "csharp_dlls.csv"),
]


# ──────────────────────────── LOGGING ────────────────────────────────────────

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ──────────────────────── STEAM WEB API ──────────────────────────────────────

def get_owned_games(api_key: str, steam_id: str) -> list[dict]:
    import requests
    url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
    params = {
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": 1,
        "include_played_free_games": 1,
    }
    logging.info("Fetching owned games from Steam Web API...")
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    games = r.json().get("response", {}).get("games", [])
    logging.info(f"  -> {len(games)} games found.")
    return games


# ──────────────────── DEPOT DOWNLOADER ───────────────────────────────────────

class LoginState:
    """Tracks whether DepotDownloader has successfully authenticated this run."""
    def __init__(self):
        self.confirmed = False  # True once we've seen a silent successful login


def fetch_manifest_only(app_id: int, username: str, exe: str, out_dir: Path,
                        login_state: LoginState) -> bool:
    """
    Run DepotDownloader -manifest-only for one app.

    If login_state.confirmed is False, runs interactively (stdin/stdout
    inherited) so password/2FA prompts are visible.  Once we confirm a silent
    login succeeded, all subsequent calls capture output quietly.

    Returns True on success.
    """
    app_out = out_dir / str(app_id)
    app_out.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        "-app", str(app_id),
        "-username", username,
        "-remember-password",
        "-manifest-only",
        "-dir", str(app_out),
    ]

    if not login_state.confirmed:
        # Interactive: let the user see and respond to any prompts
        try:
            result = subprocess.run(cmd, timeout=300)
            if result.returncode == 0:
                login_state.confirmed = True
                logging.info("  Auth confirmed - remaining apps will run silently.")
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logging.warning(f"  [app {app_id}] timed out (interactive).")
            return False
        except Exception as e:
            logging.error(f"  [app {app_id}] error: {e}")
            return False
    else:
        # Silent: capture output, check for password prompt as a safety net
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            combined = result.stdout + result.stderr

            # If Steam asked for a password it means the token expired
            if "Enter account password" in combined or "Enter your password" in combined:
                logging.warning(
                    f"  [app {app_id}] Steam asked for password - token may have expired. "
                    "Re-run without --resume to re-authenticate."
                )
                login_state.confirmed = False
                return False

            if result.returncode != 0:
                # Log first 300 chars of output to help diagnose failures
                snippet = combined.strip()[:300]
                logging.warning(f"  [app {app_id}] exit {result.returncode}: {snippet}")
                return False

            return True
        except subprocess.TimeoutExpired:
            logging.warning(f"  [app {app_id}] timed out.")
            return False
        except Exception as e:
            logging.error(f"  [app {app_id}] error: {e}")
            return False


# ─────────────────── MANIFEST PARSER ─────────────────────────────────────────
#
# DepotDownloader -manifest-only writes plain-text files named:
#   manifest_<depotId>_<manifestId>.txt
#
# File format (example):
#   Manifest ID / date     : 9059772279682119238 / 10/09/2012 15:30:33
#   Total number of files  : 42
#   Total number of chunks : 71
#   Total bytes on disk    : 123456789
#   Total bytes compressed : 98765432
#
#             Size Chunks File SHA                                 Flags Name
#        123456789      3 abc123...                                    32 bin/game.exe
#              456      1 def456...                                     0 bin/debug.pdb
#
# The filename is the last whitespace-separated field on each data line.
# Header/separator lines are identified by not having a numeric first field.

def parse_manifest_file(manifest_path: Path) -> list[str]:
    """
    Parse a DepotDownloader manifest_*.txt file and return a list of file paths.
    """
    try:
        lines = manifest_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        logging.warning(f"  Cannot read {manifest_path.name}: {e}")
        return []

    filenames = []
    in_file_list = False

    for line in lines:
        # The file list starts after the header line containing "Flags Name"
        if "Flags" in line and "Name" in line:
            in_file_list = True
            continue

        if not in_file_list:
            continue

        stripped = line.strip()
        if not stripped:
            continue

        # Each file line: <size> <chunks> <sha> <flags> <name>
        # The name is the last field and may contain spaces — but in practice
        # Steam file paths don't contain spaces (they use forward slashes).
        # Split on whitespace and take everything after the 4th field.
        parts = stripped.split()
        if len(parts) >= 5 and parts[0].isdigit():
            # Fields: size chunks sha flags name...
            filename = " ".join(parts[4:])
            filenames.append(filename.replace("\\", "/"))

    return filenames


# ──────────────────────────── DATABASE ───────────────────────────────────────

def init_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS depot_files (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id     INTEGER NOT NULL,
            app_name   TEXT,
            depot_id   TEXT,
            file_path  TEXT NOT NULL,
            scanned_at TEXT DEFAULT (datetime('now'))
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_appid    ON depot_files(app_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_filepath ON depot_files(file_path)")
    con.commit()
    return con


# ─────────────────────── FILE MATCHING ───────────────────────────────────────

def match_targets(filepath: str) -> list[int]:
    lower    = filepath.lower()
    basename = lower.rsplit("/", 1)[-1]
    matched  = []
    for i, (match_type, pattern, _) in enumerate(TARGETS):
        if   match_type == "suffix" and lower.endswith(pattern):
            matched.append(i)
        elif match_type == "exact"  and basename == pattern:
            matched.append(i)
    return matched


def extract_depot_id(manifest_path: Path) -> Optional[str]:
    """
    Extract depot ID from filename like manifest_731_123456789.txt -> "731"
    """
    name  = manifest_path.stem          # "manifest_731_123456789"
    parts = name.split("_")
    # parts = ["manifest", "731", "123456789"]
    if len(parts) >= 2 and parts[1].isdigit():
        return parts[1]
    return None


# ──────────────────────── PER-APP PROCESSING ─────────────────────────────────

def process_app(app_id: int, app_name: str,
                db_con: sqlite3.Connection,
                manifest_dir: Path) -> list[list[tuple]]:
    """
    Parse all manifests for one app.
    Inserts every file path into the DB.
    Returns hits[target_index] = [(depot_id, file_path), ...]
    """
    hits: list[list[tuple]] = [[] for _ in TARGETS]
    app_dir = manifest_dir / str(app_id)
    if not app_dir.exists():
        return hits

    manifest_files = list(app_dir.rglob("manifest_*.txt"))
    if not manifest_files:
        return hits

    all_rows = []
    for mf in manifest_files:
        depot_id  = extract_depot_id(mf)
        file_list = parse_manifest_file(mf)
        for fp in file_list:
            all_rows.append((app_id, app_name, depot_id, fp))
            for idx in match_targets(fp):
                hits[idx].append((depot_id, fp))

    if all_rows:
        db_con.executemany(
            "INSERT INTO depot_files (app_id, app_name, depot_id, file_path) "
            "VALUES (?,?,?,?)",
            all_rows,
        )
        db_con.commit()

    return hits


# ─────────────────────────── OUTPUT ──────────────────────────────────────────

def write_csvs(results_dir: Path, csv_rows: list[list]):
    headers = ["App ID", "App Name", "Depot ID", "File Path"]
    for i, (_, pattern, csv_name) in enumerate(TARGETS):
        rows = csv_rows[i]
        path = results_dir / csv_name
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerows(rows)
        logging.info(f"  {len(rows):,} rows -> {path}")


def write_report(results_dir: Path, csv_rows: list[list],
                 total_apps: int, skipped: int, elapsed: float):
    path = results_dir / "report.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("=" * 72 + "\n")
        f.write("  Steam Depot Scanner - Results Report\n")
        f.write("=" * 72 + "\n\n")
        f.write(f"  Apps scanned : {total_apps}\n")
        f.write(f"  Apps failed  : {skipped}\n")
        f.write(f"  Elapsed      : {elapsed:.0f}s\n\n")

        for i, (_, pattern, _csv) in enumerate(TARGETS):
            rows  = csv_rows[i]
            label = pattern.lstrip(".")
            bar   = "-" * max(1, 55 - len(label))
            f.write(f"-- {label.upper()}  ({len(rows)} files found) {bar}\n")
            if not rows:
                f.write("  (none found)\n\n")
                continue
            by_app: dict[str, list] = {}
            for app_id, app_name, depot_id, fp in rows:
                key = f"{app_name}  (appid {app_id})"
                by_app.setdefault(key, []).append((depot_id, fp))
            for game in sorted(by_app):
                f.write(f"\n  {game}\n")
                for depot_id, fp in sorted(by_app[game]):
                    f.write(f"    depot {(depot_id or '?'):>12}   {fp}\n")
            f.write("\n")
    logging.info(f"  Report -> {path}")


# ──────────────────────────── MAIN ───────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Steam Depot File Scanner")
    parser.add_argument(
        "--skip-fetch", action="store_true",
        help="Skip manifest download; parse whatever is already in manifests/."
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip apps whose manifest folder already exists (resume interrupted run)."
    )
    parser.add_argument(
        "--app-ids", nargs="+", type=int,
        help="Only process these App IDs (e.g. --app-ids 730 440). Good for testing."
    )
    args = parser.parse_args()

    setup_logging()

    # Validate config
    if not args.skip_fetch:
        problems = []
        if not STEAM_API_KEY:  problems.append("STEAM_API_KEY is empty")
        if not STEAM_ID:      problems.append("STEAM_ID is empty")
        if not STEAM_USERNAME: problems.append("STEAM_USERNAME is empty")
        if not Path(DEPOT_DOWNLOADER).exists():
            problems.append(f"DEPOT_DOWNLOADER path not found: {DEPOT_DOWNLOADER}")
        if problems:
            sys.exit("ERROR: Fill in the CONFIGURATION section:\n  " +
                     "\n  ".join(problems))

    MANIFEST_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)

    # Build game list
    if args.app_ids:
        games = [{"appid": a, "name": f"App {a}"} for a in args.app_ids]
    else:
        # Always fetch game names from the API so the DB has real names,
        # even in --skip-fetch mode (we just skip the DepotDownloader calls).
        if STEAM_API_KEY and STEAM_ID:
            games = get_owned_games(STEAM_API_KEY, STEAM_ID)
        else:
            # Fallback: derive app list from existing manifests/ folders
            games = [
                {"appid": int(p.name), "name": f"App {p.name}"}
                for p in MANIFEST_DIR.iterdir()
                if p.is_dir() and p.name.isdigit()
            ]
            logging.info(f"No API credentials - using {len(games)} app folder(s) from manifests/.")
        if args.skip_fetch:
            logging.info(f"--skip-fetch: skipping download, parsing {len(games)} app(s).")

    app_name_map = {g["appid"]: g.get("name", f"App {g['appid']}") for g in games}

    # Fetch manifests
    skipped    = 0
    start      = time.time()
    login_state = LoginState()

    if not args.skip_fetch:
        # Check whether the first non-skipped app will need interactive login.
        # We assume we need it unless the user explicitly says they've already
        # authenticated (they can use --skip-fetch + re-run if token is fresh).
        logging.info(f"Fetching manifests for {len(games)} apps...")
        logging.info("The first app will run interactively - enter your password/2FA when prompted.")

        for i, game in enumerate(games, 1):
            app_id   = game["appid"]
            app_name = game.get("name", str(app_id))
            app_dir  = MANIFEST_DIR / str(app_id)

            if args.resume and app_dir.exists() and any(app_dir.iterdir()):
                logging.info(f"[{i}/{len(games)}] Already done: {app_name} ({app_id})")
                continue

            suffix = "" if login_state.confirmed else " [enter password/2FA if prompted]"
            logging.info(f"[{i}/{len(games)}] {app_name} ({app_id}){suffix}")

            ok = fetch_manifest_only(app_id, STEAM_USERNAME,
                                     DEPOT_DOWNLOADER, MANIFEST_DIR,
                                     login_state)
            if not ok:
                skipped += 1

            if i < len(games):
                time.sleep(DELAY_BETWEEN_APPS)

    # Parse manifests and populate DB
    db_path = RESULTS_DIR / "all_files.db"
    logging.info(f"Building database: {db_path}")
    db_con  = init_db(db_path)

    # Also pick up any extra folders in manifests/ not in our API list
    all_app_ids = set(app_name_map)
    for p in MANIFEST_DIR.iterdir():
        if p.is_dir() and p.name.isdigit():
            all_app_ids.add(int(p.name))

    # If re-running --skip-fetch, remove existing rows for apps we are about
    # to reprocess so we don't accumulate duplicates.
    if args.skip_fetch:
        logging.info("Clearing existing DB rows for apps being reprocessed...")
        placeholders = ",".join("?" * len(all_app_ids))
        db_con.execute(f"DELETE FROM depot_files WHERE app_id IN ({placeholders})",
                       list(all_app_ids))
        db_con.commit()

    csv_rows: list[list] = [[] for _ in TARGETS]

    for app_id in sorted(all_app_ids):
        app_name = app_name_map.get(app_id, f"App {app_id}")
        hits = process_app(app_id, app_name, db_con, MANIFEST_DIR)
        for idx, entries in enumerate(hits):
            for depot_id, fp in entries:
                csv_rows[idx].append((app_id, app_name, depot_id, fp))

    db_con.close()
    elapsed = time.time() - start

    # Write outputs
    logging.info("Writing output files...")
    write_csvs(RESULTS_DIR, csv_rows)
    write_report(RESULTS_DIR, csv_rows, len(games), skipped, elapsed)

    print("\n" + "=" * 60)
    print("  SCAN COMPLETE")
    print("=" * 60)
    for i, (_, pattern, _) in enumerate(TARGETS):
        print(f"  {pattern:35s}  {len(csv_rows[i]):6,} files found")
    print(f"\n  Database -> {RESULTS_DIR / 'all_files.db'}")
    print(f"  Report   -> {RESULTS_DIR / 'report.txt'}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
