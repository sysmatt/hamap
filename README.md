# hamap

Generates an extremely high-resolution world map from an ADIF ham radio contact log.  All map rendering is fully offline once the Natural Earth data has been cached (see [Setup](#setup)).

## Features

- Parses standard ADIF files (produced by WSJT-X, Log4OM, HRD, etc.)
- Resolves contact locations from Maidenhead grid squares, explicit LAT/LON fields, or country name centroids
- Draws smooth great-circle lines from home station to each contact (when home location is known), correctly split at the date line
- Colour-codes contacts by band with a legend
- **Auto-fit extent** — by default the map is cropped to your contacts plus a margin; `--extent full` shows the whole world (Antarctica included), `--extent poles` the world without polar regions
- **Image mode** — very high-resolution PNG (48 in wide @ 300 DPI ≈ 14 400 px; height follows the map extent), zoomable to read individual callsigns
- **HTML mode** — self-contained interactive Plotly map (zoomable, pannable, clickable popups) with no external dependencies
- `--preview` mode opens the image in an interactive window instead of saving (image mode only)
- Offline-first: map data is cached under `~/.hamap/` after first download

## Install

hamap installs as a command-line tool with [pipx](https://pipx.pypa.io/):

```bash
pipx install .            # from a checkout of this repo
pipx install --force .    # upgrade after pulling changes
```

For development, an editable install picks up source edits immediately:

```bash
pipx install -e .
# or, in a local virtualenv:
python3 -m venv .venv && .venv/bin/pip install -e .
```

## Setup

Pre-download the Natural Earth shapefiles (requires internet, only needed once):

```bash
hamap --setup
```

The data is cached to `~/.hamap/cartopy/` and all subsequent runs work fully offline.

## Usage

```
hamap [FILE] [OPTIONS]

Positional:
  FILE                  ADIF log file to map

Output mode (mutually exclusive, default is image):
  --image               Generate a PNG image (default)
  --html                Generate a self-contained interactive HTML map (Plotly)

Options:
  --output FILE, -o     Output file path (default: <input>.png or <input>.html)
  --preview             Open image in a window instead of saving (image mode only)
  --my-grid GRID        Home station Maidenhead grid square (e.g. EN82)
  --no-lines            Skip great-circle lines
  --line-alpha A        Great-circle line opacity 0..1 (default: auto — 0.45 for
                        small logs, fading toward 0.12 as contacts grow)
  --no-labels           Skip callsign labels (image mode)
  --dpi N               Output DPI (default: 300)
  --width N             Figure width in inches (default: 48); height follows the extent
  --extent MODE         auto  = fit contacts + home, plus a margin (default)
                        full  = whole world, poles included
                        poles = whole world, polar regions trimmed (60°S–84°N)
  --font-size PT        Info box font size in points (default: 3.0)
  --group-by MODE       grid   = one info box per grid square (default)
                        entity = one box per US state / Canadian province,
                                 otherwise per country; one dot per region,
                                 region tinted in its dominant band colour
  --box-calls N         Callsigns per box: omit for all; N = the N busiest plus
                        a "+k more" footer; 0 = summary (calls, grids, QSOs per band)
  --ocean-boxes         Prefer open water for info boxes when it isn't a big
                        detour, freeing land for inland boxes (experimental)
  --ocean-reach DEG     Max distance a box may move to reach open water (default: 8)
  --truncate-grids      Reduce grid squares to 4-char accuracy before grouping
  --label-countries     Draw country name labels at their geographic centroids
  --label-states        Draw US state / Canadian province labels and borders
  --dxcc                DXCC mode: shade LoTW-confirmed entities, one box per entity
  --setup               Download offline map data to ~/.hamap/ and exit

Filtering (applied before rendering, in this order):
  --start DATE          Only include QSOs on or after DATE (YYYY-MM-DD or YYYYMMDD)
  --end DATE            Only include QSOs on or before DATE
  --tail-days N         Only include QSOs from the last N days   [mutually exclusive]
  --tail N              Only use the last N QSOs in the file      [mutually exclusive]

Verbosity:
  --verbose / --debug / --trace
  --logfile FILE        Also write log to FILE
  --syslog              Also send log to syslog
```

### Examples

```bash
# Save a PNG map alongside the ADIF file (default)
hamap wsjtx_log.adi

# Interactive HTML map
hamap wsjtx_log.adi --html

# HTML map with state/province borders and labels
hamap wsjtx_log.adi --html --label-states

# Only QSOs from a specific date range
hamap wsjtx_log.adi --start 2025-01-01 --end 2025-03-31

# Last 30 days of activity
hamap wsjtx_log.adi --tail-days 30

# Only the most recent 500 log entries
hamap wsjtx_log.adi --tail 500

# Custom output path
hamap wsjtx_log.adi --output ~/Desktop/contacts.png
hamap wsjtx_log.adi --html --output ~/Desktop/contacts.html

# Interactive preview window (image mode only)
hamap wsjtx_log.adi --preview

# With home station grid square (enables great-circle lines)
hamap wsjtx_log.adi --my-grid EN82

# Whole-world view instead of auto-fit
hamap wsjtx_log.adi --extent full

# Larger / higher-resolution image output
hamap wsjtx_log.adi --width 64 --dpi 400

# Skip labels for very busy maps
hamap wsjtx_log.adi --no-labels

# Just dots, no great-circle lines
hamap wsjtx_log.adi --no-lines
```

## Large Logs

hamap handles logs with thousands of QSOs, but box placement is ultimately limited by how many pixels each degree of map gets. Tips:

- **`--truncate-grids`** groups contacts by 4-character grid, which usually cuts the number of boxes by about 40%.
- **Go wider.** Canvas width is the biggest lever for placement quality. On a 1,600-QSO log, going from `--width 48` to `--width 64` halved the total leader-line length, and `--width 80` cut it by about 55%. The cost is memory: roughly 1.1 GB at 48 in, rising with the square of the width.
- **`--group-by entity`** consolidates by US state / Canadian province / country (e.g. 568 grid boxes → 184 entity boxes). Each region gets a single dot at the centroid of the area actually worked (so "Russia" anchors in European Russia when that's where the contacts are), one great-circle line, and one box; the region itself is tinted in its dominant band colour, which doubles as a worked-entities map. The state comes from the ADIF `STATE` field, falling back to the Natural Earth province shapes (with nearest-province matching for grid centres that land in lakes or offshore).
- **`--box-calls N`** caps box size: `--box-calls 12` lists the 12 busiest callsigns plus "+k more"; `--box-calls 0` replaces the list with a summary (callsigns, grids, QSOs per band). Works in both grid and entity modes.
- **`--ocean-boxes`** (experimental) moves crowded coastal boxes offshore so inland regions keep room near their dots. A box goes to sea only if its whole rectangle clears land and the open-water spot is no more than 2.5× as far as the nearest free spot of any kind, so boxes with room beside their dot stay put. It works best with `--group-by entity`; with grid boxes it tends to build a wall of boxes along coastlines.
- `--line-alpha 0.08` (or `--no-lines`) if the great-circle fan still dominates.
- `--verbose` prints placement statistics (median / p90 / max leader length) so you can compare settings.

Box placement places the most crowded areas first, then searches outward from each dot in rings, hugging the dot edge-to-edge in each direction.

## Interactive HTML Map

The `--html` mode produces a fully self-contained HTML file — no internet connection or external libraries needed to open it.

### Navigation

- **Scroll / pinch** to zoom
- **Click and drag** on the map background to pan
- **Hover** over any contact dot to see a popup with QSO details
- Use the Plotly toolbar (top-right) to reset the view

### Clickable Popups

Clicking a contact dot **pins a popup** that stays visible until dismissed, making it easy to take annotated screenshots with multiple locations highlighted simultaneously.

Each popup shows:
- Grid square or location header, QSO count
- Band(s) and date range
- Countries worked
- Per-QSO detail rows: **callsign**, operator name, date, time (UTC), city/QTH, band

Up to 25 QSOs are listed per popup; busier grids show a "… and N more" indicator.

**To dismiss** a popup: click the same dot again, or click the **×** button in the popup's title bar.

**To reposition** a popup: grab its **title bar** and drag it anywhere on the map.  Multiple popups can be open and repositioned independently.

### Legend and Statistics

- **Band legend** — lower-right corner; click a band name to toggle that band's dots
- **Stats box** — lower-left corner; shows total contacts, unique grids, and unique callsigns

## Location Resolution

Contacts are placed on the map in this priority order:

1. **Explicit LAT / LON** fields in the ADIF record
2. **GRIDSQUARE** — Maidenhead locator (4 or 6 characters), converted algorithmically
3. **COUNTRY** — matched against a built-in centroid dictionary (~240 entities)

Contacts with none of the above are skipped with a `[DEBUG]` message.  For FT8/FT4/JT65 logs from WSJT-X nearly every record includes a grid square, so skip rates are typically zero.

## Map Style

- Dark navy ocean, dark warm-sand land (so band colours, especially 20 m green, stand out), Natural Earth 10 m coastlines and borders
- Contacts colour-coded by band (160 m = red → 10 m = purple → VHF = pink/grey), dots outlined so they stand out over lines
- Home station shown as a yellow star
- Info boxes: grid square header in the band colour, callsigns in white, monospace, sized for zooming (image mode); grids with more than 6 callsigns wrap into columns
- Great-circle lines fade and thin automatically as the number of contacts grows; the most common band is drawn first so rarer bands stay visible on top
- Country / state labels skip anything that would overlap a label already drawn (states first, then countries largest-first); micro-states are not labelled
- Stats box (station call, counts, first/last QSO date, active filters) and band legend scale with the canvas and are placed in the map corners with the fewest contacts

## Offline Data

All cached data lives under `~/.hamap/`:

| Path | Contents |
|------|----------|
| `~/.hamap/cartopy/` | Natural Earth shapefiles (cartopy cache) |

No API keys, no internet required after `--setup`.

## Dependencies

| Package | Purpose |
|---------|---------|
| `matplotlib` | Figure rendering (image mode) |
| `cartopy` | Geographic projections and Natural Earth features |
| `numpy` | Array operations (pulled in by matplotlib/cartopy) |
| `shapely` | Geometry handling (DXCC fills, land mask) |
| `plotly` | Interactive HTML map (HTML mode) |
