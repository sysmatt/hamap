# hamap

Generates an extremely high-resolution world map from an ADIF ham radio contact log.  All map rendering is fully offline once the Natural Earth data has been cached (see [Setup](#setup)).

## Features

- Parses standard ADIF files (produced by WSJT-X, Log4OM, HRD, etc.)
- Resolves contact locations from Maidenhead grid squares, explicit LAT/LON fields, or country name centroids
- Draws great-circle lines from home station to each contact (when home location is known)
- Colour-codes contacts by band with a legend
- **Image mode** — very high-resolution PNG (default 24 × 12 in @ 300 DPI ≈ 7200 × 3600 px, zoomable to read individual callsigns)
- **HTML mode** — self-contained interactive Plotly map (zoomable, pannable, clickable popups) with no external dependencies
- `--preview` mode opens the image in an interactive window instead of saving (image mode only)
- Offline-first: map data is cached under `~/.hamap/` after first download

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Pre-download the Natural Earth shapefiles (requires internet, only needed once):

```bash
./hamap --setup
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
  --no-labels           Skip callsign labels (image mode)
  --dpi N               Output DPI (default: 300)
  --width N             Figure width in inches (default: 24)
  --height N            Figure height in inches (default: 12)
  --font-size PT        Label font size in points (default: 3.0)
  --truncate-grids      Reduce grid squares to 4-char accuracy before grouping
  --label-countries     Draw country name labels at their geographic centroids
  --label-states        Draw US state / Canadian province labels and borders
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
./hamap wsjtx_log.adi

# Interactive HTML map
./hamap wsjtx_log.adi --html

# HTML map with state/province borders and labels
./hamap wsjtx_log.adi --html --label-states

# Only QSOs from a specific date range
./hamap wsjtx_log.adi --start 2025-01-01 --end 2025-03-31

# Last 30 days of activity
./hamap wsjtx_log.adi --tail-days 30

# Only the most recent 500 log entries
./hamap wsjtx_log.adi --tail 500

# Custom output path
./hamap wsjtx_log.adi --output ~/Desktop/contacts.png
./hamap wsjtx_log.adi --html --output ~/Desktop/contacts.html

# Interactive preview window (image mode only)
./hamap wsjtx_log.adi --preview

# With home station grid square (enables great-circle lines)
./hamap wsjtx_log.adi --my-grid EN82

# Larger / higher-resolution image output
./hamap wsjtx_log.adi --width 40 --height 20 --dpi 400

# Skip labels for very busy maps
./hamap wsjtx_log.adi --no-labels

# Just dots, no great-circle lines
./hamap wsjtx_log.adi --no-lines
```

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

- Dark navy background, dark-green land, Natural Earth 10 m coastlines and borders
- Contacts colour-coded by band (160 m = red → 10 m = purple → VHF = pink/grey)
- Home station shown as a yellow star
- Semi-transparent callsign + grid labels, monospace font, sized for zooming (image mode)

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
| `plotly` | Interactive HTML map (HTML mode) |
