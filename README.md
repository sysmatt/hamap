# hamap

Generates an extremely high-resolution world map from an ADIF ham radio contact log.  All map rendering is fully offline once the Natural Earth data has been cached (see [Setup](#setup)).

## Features

- Parses standard ADIF files (produced by WSJT-X, Log4OM, HRD, etc.)
- Resolves contact locations from Maidenhead grid squares, explicit LAT/LON fields, or country name centroids
- Draws great-circle lines from home station to each contact (when home location is known)
- Colour-codes contacts by band with a legend
- Outputs a very high-resolution PNG (default 24 × 12 in @ 300 DPI ≈ 7200 × 3600 px — zoomable to read individual callsigns)
- `--preview` mode opens the map in an interactive window instead of saving
- Offline-first: map tile data is cached under `~/.hamap/` after first download

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

Options:
  --output FILE, -o     Output PNG path (default: <input>.png)
  --preview             Open map in a window instead of saving
  --my-grid GRID        Home station Maidenhead grid square (e.g. EN82)
  --no-lines            Skip great-circle lines
  --no-labels           Skip callsign labels
  --dpi N               Output DPI (default: 300)
  --width N             Figure width in inches (default: 24)
  --height N            Figure height in inches (default: 12)
  --font-size PT        Label font size in points (default: 3.0)
  --truncate-grids      Reduce grid squares to 4-char accuracy before grouping
  --label-countries     Draw country name labels at their geographic centroids
  --label-states        Draw US state / Canadian province labels at their centroids
  --html                Also write an HTML file with interactive contact popups
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
# Save map alongside the ADIF file
./hamap wsjtx_log.adi

# Only QSOs from a specific date range
./hamap wsjtx_log.adi --start 2025-01-01 --end 2025-03-31

# Last 30 days of activity
./hamap wsjtx_log.adi --tail-days 30

# Only the most recent 500 log entries
./hamap wsjtx_log.adi --tail 500

# Custom output path
./hamap wsjtx_log.adi --output ~/Desktop/contacts.png

# Interactive preview
./hamap wsjtx_log.adi --preview

# With home station grid square (enables great-circle lines)
./hamap wsjtx_log.adi --my-grid EN82

# Larger / higher-resolution output
./hamap wsjtx_log.adi --width 40 --height 20 --dpi 400

# Skip labels for very busy maps
./hamap wsjtx_log.adi --no-labels

# Just dots, no lines, for a cleaner look
./hamap wsjtx_log.adi --no-lines
```

## Location Resolution

Contacts are placed on the map in this priority order:

1. **Explicit LAT / LON** fields in the ADIF record
2. **GRIDSQUARE** — Maidenhead locator (4 or 6 characters), converted algorithmically
3. **COUNTRY** — matched against a built-in centroid dictionary (~240 entities)

Contacts with none of the above are skipped with a `[DEBUG]` message.  For FT8/FT4/JT65 logs from WSJT-X nearly every record includes a grid square, so skip rates are typically zero.

## Map Style

- Dark navy background, dark-green land, with Natural Earth 10 m coastlines and borders
- Contacts colour-coded by band (160 m = red → 10 m = purple → VHF = pink/grey)
- Home station shown as a yellow star
- Semi-transparent callsign + grid labels, monospace, sized for zooming

## Offline Data

All cached data lives under `~/.hamap/`:

| Path | Contents |
|------|----------|
| `~/.hamap/cartopy/` | Natural Earth shapefiles (cartopy cache) |

No API keys, no internet required after `--setup`.

## Dependencies

| Package | Purpose |
|---------|---------|
| `matplotlib` | Figure rendering |
| `cartopy` | Geographic projections and Natural Earth features |
| `numpy` | Array operations (pulled in by matplotlib/cartopy) |

Logging uses the `AppLogger` class from the sibling `../qrzlog/qrzlog` project.
