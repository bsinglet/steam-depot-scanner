# Steam Depot Scanner

Scans your entire Steam library for specific files in game depots — **without downloading any game content**. Fetches only the file manifests (indexes), stores every file path in a local SQLite database, and lets you query it however you like.

Useful for finding games that ship PDB debug symbols, Unity `Assembly-CSharp.dll` files, or any other file type across your whole library.

---

## Requirements

- Python 3.10+
- `requests` library: `pip install requests`
- DepotDownloader (see below)
- A Steam account + Steam Web API key

---

## Setup

### 1. Install DepotDownloader

The easiest way on Windows is via winget:

```powershell
winget install SteamRE.DepotDownloader
```

After installation the exe will be at a path like:

```
%APPDATA%\Local\Microsoft\WinGet\Packages\SteamRE.DepotDownloader_Microsoft.Winget.Source_8wekyb3d8bbwe\DepotDownloader.exe
```

The default `DEPOT_DOWNLOADER` path in `steam_depot_scanner.py` is already set to this location.

---

### 2. Get a Steam Web API Key

1. Go to https://steamcommunity.com/dev/apikey
2. Log in and enter any name for the domain (e.g. `localhost`)
3. Copy the key — it looks like `A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6`

---

### 3. Find Your SteamID64

1. Open the Steam client
2. Click your username in the top-right corner
3. Choose **Account Details**
4. Your Steam ID is the long number displayed directly below your username on that page — it looks like `76561198012345678`

---

### 4. Find Your Steam Username

Your Steam username is the account name you type when logging in — **not** your display name. It is also shown on the **Account Details** page (same page as your Steam ID), labeled **Account name**.

---

### 5. Configure `steam_depot_scanner.py`

Open `steam_depot_scanner.py` and fill in the `CONFIGURATION` block near the top:

```python
STEAM_API_KEY    = "A1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6"
STEAM_ID         = "76561198012345678"
STEAM_USERNAME   = "your_login_name"
DEPOT_DOWNLOADER = r"C:\path\to\DepotDownloader.exe"  # winget default is already set
```

You can also tune the delay between requests. The default is 30 seconds, which is conservative but reliable for large libraries. If you get `RateLimitExceeded` errors, increase it:

```python
DELAY_BETWEEN_APPS = 30.0
```

---

## Running the Scanner

```powershell
python steam_depot_scanner.py
```

On the **first run**, DepotDownloader will prompt for your Steam password and Steam Guard 2FA code directly in the terminal. Type them and press Enter. After that, your session token is saved and all subsequent apps run silently and automatically.

### Flags

| Flag | Description |
|---|---|
| `--resume` | Skip apps whose manifest folder already exists. Use this after an interruption. |
| `--skip-fetch` | Don't call DepotDownloader — just re-parse manifests already on disk and rebuild the database. |
| `--app-ids 730 440` | Only process specific App IDs. Useful for testing. |

### Typical workflow for a large library

```powershell
# First run — prompts for password/2FA once, then runs unattended
python steam_depot_scanner.py

# If interrupted (Ctrl+C is safe — all progress is saved on disk), resume with:
python steam_depot_scanner.py --resume

# Already have all manifests and just want to rebuild the database:
python steam_depot_scanner.py --skip-fetch
```

### If you get `RateLimitExceeded` errors

Steam rate-limits manifest requests. Press Ctrl+C (your progress is safe), wait 10-15 minutes, then run with `--resume`. If it keeps happening, increase `DELAY_BETWEEN_APPS` in the config.

---

## Outputs

After a run, the following are written to the `results/` folder:

| File | Contents |
|---|---|
| `all_files.db` | SQLite database of **every file path** from every depot scanned |
| `pdb_files.csv` | All `.pdb` files found across your library |
| `csharp_dlls.csv` | All `Assembly-CSharp.dll` files found (Unity games) |
| `report.txt` | Human-readable summary grouped by game |

The database contains everything. The CSVs are just pre-filtered views of it. You can query the database at any time for any file type without re-scanning.

### Adding more file types to the pre-generated CSVs

Edit the `TARGETS` list in `steam_depot_scanner.py`, then run with `--skip-fetch` to regenerate without hitting Steam:

```python
TARGETS = [
    ("suffix", ".pdb",                "pdb_files.csv"),
    ("exact",  "assembly-csharp.dll", "csharp_dlls.csv"),
    ("suffix", ".exe",                "exe_files.csv"),   # add new entries here
    ("suffix", ".pdf",                "pdf_files.csv"),
]
```

---

## Querying Results with `query_results.py`

All queries run against the local database — no internet connection needed.

### Summary

```powershell
python query_results.py
```

Shows total file count, number of games, and number of depots in the database.

---

### Filter by file type

```powershell
# All .pdb files
python query_results.py --filter .pdb

# All executables
python query_results.py --filter .exe

# All PDFs
python query_results.py --filter .pdf

# All MP3s
python query_results.py --filter .mp3

# Exact filename match (case-insensitive)
python query_results.py --filter assembly-csharp.dll
python query_results.py --filter readme.txt
```

---

### Search by game name (fuzzy)

You don't need to know the exact internal Steam name. The matcher checks word substrings and acronyms:

```powershell
# Partial word — matches "Half-Life 2", "Half-Life 2: Lost Coast", etc.
python query_results.py --game "half life"

# Acronym — matches all Counter-Strike titles
python query_results.py --game cs

# More specific acronym — matches only Counter-Strike 2
python query_results.py --game cs2

# Handles punctuation — matches "Garry's Mod"
python query_results.py --game "garry mod"
```

---

### Search by file path substring

```powershell
# Find anything in a "symbols" folder
python query_results.py --search symbols

# Find files related to the crash reporter
python query_results.py --search crashreport

# Find anything under Engine/Binaries
python query_results.py --search Engine/Binaries
```

---

### Stack filters together

All filters are ANDed — combine any of `--game`, `--filter`, `--search`, and `--app`:

```powershell
# .pdb files in any game matching "portal"
python query_results.py --game portal --filter .pdb

# All files for a specific App ID
python query_results.py --app 620 --filter .dll

# .exe files with "editor" in the path, across all games
python query_results.py --filter .exe --search editor

# PDBs for a specific game narrowed to a subfolder
python query_results.py --game biped --filter .pdb --search Win64
```

---

### Browse all games in the database

```powershell
# Alphabetical list with file counts
python query_results.py --list-games

# File count leaderboard (most files first)
python query_results.py --stats
```

---

### Get all depots for a game

```powershell
python -c "
import sqlite3
con = sqlite3.connect('results/all_files.db')
rows = con.execute('''
    SELECT DISTINCT depot_id, COUNT(*) as file_count
    FROM depot_files
    WHERE app_id = 730
    GROUP BY depot_id
    ORDER BY depot_id
''').fetchall()
for depot_id, count in rows:
    print(f'  Depot {depot_id}  ({count} files)')
con.close()
"
```

Replace `730` with the App ID of the game you want.

---

### Export any query to CSV

Add `--csv filename.csv` to any query:

```powershell
python query_results.py --filter .pdb --csv pdb_results.csv
python query_results.py --game "half life" --filter .pdb --csv hl_pdbs.csv
python query_results.py --stats --csv game_file_counts.csv
```

---

### Deduplicate the database

If you have run `--skip-fetch` multiple times and suspect duplicate rows:

```powershell
python -c "
import sqlite3
con = sqlite3.connect('results/all_files.db')
con.execute('''
    DELETE FROM depot_files
    WHERE id NOT IN (
        SELECT MIN(id) FROM depot_files
        GROUP BY app_id, depot_id, file_path
    )
''')
con.commit()
removed = con.execute('SELECT changes()').fetchone()[0]
print(f'Removed {removed} duplicate rows')
con.close()
"
```

---

## File Structure

```
steam_depot_scanner/
├── steam_depot_scanner.py     Main scanner
├── query_results.py           Query tool
├── README.md                  This file
├── scanner.log                Full run log
├── manifests/                 Raw manifest files from DepotDownloader
│   ├── 730/                   One folder per App ID
│   │   ├── manifest_731_....txt
│   │   └── manifest_732_....txt
│   └── ...
└── results/
    ├── all_files.db           SQLite database (the main asset)
    ├── pdb_files.csv
    ├── csharp_dlls.csv
    └── report.txt
```

The `manifests/` folder is the raw data. The `results/` folder is fully derived from it and can be regenerated at any time with `--skip-fetch`.
