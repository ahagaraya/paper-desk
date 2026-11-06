# ahagaraya · paper desk

Personal paper-trading simulation dashboard. Static files only: `index.html` and `data.json`. There is no trading backend on this site, and the page cannot place orders.

The badges **PAPER**, **SIMULATION**, and **LIVE OFF** describe the book. If a future snapshot ever carries `live: true`, the page shows **LIVE ON** and still cannot send orders.

`data.json` in this repository is a sanitized historical snapshot (published `2026-11-07T00:40:10+03:00`). It was imported and passed through `publish_data.py`. It is not a later reading of the paper host. The trading state directory was not available when this copy was prepared. The page marks an old export as aging or stale instead of presenting it as a market feed.

PnL is **cumulative since $1,000**. It is equity minus that fixed start. It is not the midnight day figure, which can reset to zero while the account is still away from $1,000.

## Files

| File | Purpose |
| --- | --- |
| `index.html` | Dark mobile terminal. Reloads `./data.json` every 30 seconds. |
| `data.json` | Sanitized snapshot: equity, cash, PnL since $1,000, positions, closes, risk, timestamps. |
| `publish_data.py` | Rebuilds `data.json` from local paper state. |
| `upload_data.py` | Updates only `data.json` on `main` through the GitHub contents API. |
| `desk_snapshot.py` | Shared whitelist, hash, and validation. |

The publisher reads `equity.json`, `risk_status.json`, and `positions.json`. It writes the snapshot beside the script, or to `--out`. It does not edit trading configuration and it does not place orders. Unknown fields, paths, raw logs, and credentials are dropped. If those three files are missing, it exits without writing.

## Preview locally

```bash
python3 -m unittest discover -s tests -v
python3 -m http.server 8765
```

Open `http://127.0.0.1:8765/`. Use a phone width as well as a desktop window. `file://` can block `fetch`; the page then keeps the cached snapshot shipped inside `index.html` and says so.

## GitHub Pages

This site is meant to be served as static files from the repository root. `.nojekyll` is included so Jekyll does not rewrite the tree. Asset URLs are relative (`./data.json`), which works at `https://ahagaraya.github.io/paper-desk/`.

These steps were not completed from the environment that prepared the branch. The API credential available there could read the repository and could not administer Pages. Do not treat the site as deployed until the checks below return HTTP 200.

1. Merge the pull request into `main`.
2. Open `https://github.com/ahagaraya/paper-desk/settings/pages`.
3. Under **Build and deployment**, set **Source** to **Deploy from a branch**.
4. Set **Branch** to `main` and the folder to `/ (root)`.
5. Save.
6. Wait until the Pages panel shows the site is published. The first build often takes a few minutes.
7. Confirm both of these return HTTP 200, with `data.json` as JSON and the HTML containing the paper labels:

```bash
curl -sI https://ahagaraya.github.io/paper-desk/
curl -sI https://ahagaraya.github.io/paper-desk/data.json
```

No custom domain is configured. No paid host is required.

## Refresh the public snapshot

Run this on the machine that already has the paper state and an authorized `gh` login. The scripts do not take a token argument, and the repository does not contain one. `gh` uses its existing credential store.

```bash
python3 publish_data.py --state /workspace/trading-paper/state --out data.json
python3 upload_data.py --file data.json
```

`upload_data.py` calls the documented contents endpoint and nothing else:

`PUT /repos/ahagaraya/paper-desk/contents/data.json`

The allowlist is exact: repository `ahagaraya/paper-desk`, branch `main`, path `data.json`. Any other repository, branch, or file name is rejected. The file must already pass the public whitelist. A second run with the same bytes does nothing. If the metrics are unchanged and the remote `published_at` is still inside `--max-age` seconds (default 240), the upload is skipped. If the metrics changed, or the remote publish is older than that window, the file is updated so the page can show a new timestamp.

Check without writing or uploading:

```bash
python3 publish_data.py --check data.json
python3 upload_data.py --file data.json --check
python3 upload_data.py --file data.json --dry-run
```

### Schedule on the paper host

A five-minute refresh is the intended host routine. It is **not** installed by this repository.

```cron
*/5 * * * * python3 /path/to/publish_data.py --state /workspace/trading-paper/state --out /path/to/data.json && python3 /path/to/upload_data.py --file /path/to/data.json
```

Copy `publish_data.py`, `upload_data.py`, and `desk_snapshot.py` onto that machine together. They do not need a git clone. The `gh` user needs contents write on this repository. After each upload, GitHub Pages rebuilds from `main`; the page itself requests `data.json` every 30 seconds. The export looks fresh for 10 minutes, aging until 30 minutes, then stale.

The page clock and the export clock are different. "Snapshot file loaded" means this browser reached `data.json`. The export and source lines say how old the figures are.
