# handle-finder

A command-line tool that finds **short Instagram usernames (3 and 4 characters) that are likely available**. It generates candidates, checks them slowly and politely, ranks the survivors, and exports them to CSV.

> **Nothing this tool reports is a guarantee.** A handle is only registrable if Instagram's own signup page accepts it and you finish registering. Even "CONFIRMED_AVAILABLE" can change a minute later.

## ⚠️ Terms of Use notice

**Automated querying may violate Instagram's Terms of Use. Use small batches, at your own risk.** The tool prints this notice every time it starts.

The tool is deliberately conservative:

- It **never logs in** during the pre-filter, and never creates an account.
- It throttles every request (3–6 s with random jitter by default) and has a hard daily cap (500 by default).
- When Instagram pushes back, it backs off. After 3 failures in a row it **stops and tells you to wait**.
- It does **not** use proxy rotation, IP spoofing, captcha solving, fingerprint spoofing or any other way around Instagram's limits, and it never will.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -e .
```

Only needed for `--confirm browser`:

```bash
playwright install chromium
```

`pip install -r requirements.txt` also works if you'd rather run it as `python -m handle_finder`.

## Usage

### End to end

```bash
handle-finder run --length 4 --mode pronounceable --limit 50
```

This generates 50 candidates, pre-filters them, prints the possibly-available shortlist for you to verify, and ranks every survivor in the database. It writes `candidates.txt`, `results.csv` and `ranked.csv`.

### Step by step

```bash
handle-finder generate --length 3 --charset letters --mode words --limit 100
```

```bash
handle-finder check --input candidates.txt --output results.csv
```

```bash
handle-finder rank --input results.csv --top 100
```

Right before you register, re-verify your shortlist:

```bash
handle-finder recheck --input ranked.csv
```

### Generation modes

| `--mode`        | What it produces                                                                                                   |
|-----------------|--------------------------------------------------------------------------------------------------------------------|
| `all`           | Every combination of `--charset`, shuffled. Large spaces are randomly sampled.                                     |
| `pronounceable` | CVC (3 letters) and CVCV / VCVC / CCVC (4 letters) built from real consonant/vowel rules. No `xqzt`-style clusters. |
| `words`         | 3/4-letter English words from `handle_finder/data/words.txt`, plus common short names and brand-style fragments.  |

`--charset` (`letters`, `alnum`, `alnum_sep`) only applies to `all`; the other two modes produce letters only.

Every candidate is checked against Instagram's rules (`a-z 0-9 . _`, lowercase, no `..`, no leading or trailing `.`) **before** any network call. Names that are already checked are skipped automatically, so rerunning a command resumes where it left off.

### Confirmation (Stage 3)

Confirmation runs only on names the pre-filter marked `POSSIBLY_AVAILABLE`. Choose it with `--confirm`:

- `--confirm manual` (default for `run` and `recheck`): prints the shortlist with links and instructions. Add `--ask` to answer y/n for each name and have your answers saved.
- `--confirm browser`: opens a **visible** Chromium window with a persistent profile (`~/.handle_finder/browser-profile`). For each name it opens the signup page, types the name into the Username field, presses Tab so Instagram validates it, and reads the result. It **never submits the form, never presses Enter, and never clicks a button.** If it can't read the result, it asks you to look at the window. Don't log in with this profile; if it's logged in, the signup page redirects and the tool stops with an explanation.
- `--confirm none` (default for `check`): skip confirmation.

`--browser-path /path/to/chrome` uses a specific Chrome/Chromium instead of Playwright's download.

### Useful options

| Option                       | Default       | Meaning                                                              |
|------------------------------|---------------|----------------------------------------------------------------------|
| `--delay-min` / `--delay-max`| 3 / 6         | Seconds between requests (random value in between; minimum allowed 1) |
| `--daily-cap`                | 500           | Hard limit on requests per day, counted across all runs               |
| `--backoff`                  | 60            | First backoff after a rate limit; doubles on each consecutive failure |
| `--force`                    | off           | `check`: re-check names that already have a result                    |
| `--db`                       | `handles.db`  | SQLite file (or set `HANDLE_FINDER_DB`)                               |
| `--seed`                     | random        | Reproducible candidate generation                                     |

Run `handle-finder <command> --help` for the full list.

## How results are interpreted

The pre-filter requests `https://www.instagram.com/<name>/` while logged out, with an ordinary browser User-Agent:

| Response                                                   | Status                                                         |
|------------------------------------------------------------|----------------------------------------------------------------|
| 200 with profile markers                                   | `TAKEN`                                                        |
| 404, or 200 "Sorry, this page isn't available"             | `POSSIBLY_AVAILABLE` (could still be banned, reserved or deactivated) |
| 429, 403, redirect to login/challenge, "please wait" page  | `RATE_LIMITED`: back off and retry the same name, stop after 3 in a row |
| Anything else (ambiguous page, 5xx, network error)         | `UNKNOWN`: never treated as available                          |

Other safety rules:

- An inconclusive result (`UNKNOWN`, `RATE_LIMITED`) never overwrites a conclusive one.
- 10 ambiguous responses in a row also stop the run, in case Instagram changed its pages.

All statuses: `UNCHECKED`, `TAKEN`, `POSSIBLY_AVAILABLE`, `CONFIRMED_AVAILABLE`, `UNKNOWN`, `RATE_LIMITED`.

## Ranking (0–100)

| Component        | Max | Rule                                                                  |
|------------------|-----|-----------------------------------------------------------------------|
| Length           | 30  | 3 chars = 30, 4 chars = 20                                            |
| Word / name      | 25  | English word 25, known name 22, brand-style fragment 15               |
| Pronounceability | 20  | Vowel/consonant pattern and realistic consonant clusters              |
| Characters       | 15  | Letters only 15, with digits 7, with `.` or `_` 3                     |
| No look-alikes   | 10  | Penalties for `0`/`1`, mixing `l/1/i` or `0/o`, and `rn`→m, `vv`→w, `cl`→d |

Only `POSSIBLY_AVAILABLE` and `CONFIRMED_AVAILABLE` handles are ranked. Ties go to confirmed handles.

## Output files

- `results.csv` (from `check`/`run`): `handle,status,score,reason,checked_at,notes` for every candidate.
- `ranked.csv` (from `rank`/`run`): `handle,status,score,reason,checked_at` for the top survivors.
- `handles.db`: SQLite table `handles(name PRIMARY KEY, status, checked_at, source, notes)`, plus `check_log` for the daily cap. Every result is committed the moment it arrives, so a crash or Ctrl+C loses nothing.

## Exit codes

| Code | Meaning                                                  |
|------|----------------------------------------------------------|
| 0    | Finished (or paused at the daily cap)                    |
| 1    | Bad input or setup problem (message explains what to do) |
| 3    | Stopped because Instagram is blocking or responses were unclear. Wait before retrying |
| 130  | Stopped by you (Ctrl+C); progress is saved               |

## Limitations

- **404 doesn't mean available.** Banned, reserved, recently deactivated and some policy-blocked names also return "not found". Many short handles fall into these groups.
- **Instagram changes its pages.** The profile, not-found and login-wall markers are heuristics. If Instagram changes its HTML, results may come back `UNKNOWN`. The tool then stops after 10 in a row rather than guessing.
- **Logged-out access is heavily limited.** From many networks, especially cloud and VPN IP ranges, Instagram answers with login walls or 429s almost immediately. The tool will then stop and ask you to wait; it won't try to get around the block.
- **The browser checker reads a page built for humans.** It looks for the "username isn't available" message and the field's accepted/error indicator. If neither is clear, it asks you rather than guessing. It needs a desktop session; it won't run headless.
- **The daily cap is per local calendar day** and counts every request (including retries and browser checks) recorded in the database you point at. Using a different `--db` resets it, so don't.
- **The wordlists are small and hand-curated** (`handle_finder/data/*.txt`); edit them to suit you.

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

The tests cover the rules, the generator, the ranker, status interpretation (with mocked HTTP responses via `httpx.MockTransport`), throttling and backoff, the daily cap, resume and crash safety, the confirmation flow, and the CLI end to end. None of them touch the network.
