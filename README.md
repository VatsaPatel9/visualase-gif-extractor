# Visualase GIF Extractor

A small local web app that reads Visualase MR-thermometry GIF exports and turns them into data:

* **Every frame:** time, run, laser on/off, power %, watts.
* **Every numbered point:** temperature (°C), its limit (<90 / <50), and its position on the image.
* **Distance of each point from the laser fiber** (the dark "stick"). It is measured from the fiber **center**, the midpoint between where the fiber enters the skull and its tip, and also from the tip.
* **Output:** Excel (with live distance formulas) or CSV, plus an **annotated GIF** with everything drawn on top.

Everything runs on your computer. The server only listens on `127.0.0.1`, and no file is sent anywhere.

## Start it

```bash
cd visualase_app
./run.sh
```

The first run creates a private Python environment in `.venv` and installs the requirements (about 1 minute). After that it starts immediately and opens <http://127.0.0.1:8790> in your browser. Press `Ctrl+C` in the terminal to stop it.

Options: `./run.sh --port 9000` (if 8790 is busy), `./run.sh --no-browser`.

Requires Python 3.10+ (tested with 3.14). If LibreOffice is installed, exported Excel files also carry pre-computed values for Quick Look and other previewers. Excel recalculates on open either way.

## Using it

1. **Add GIFs.** Drag them onto the drop zone; several at once is fine. Each file is read frame by frame, which takes about 5–20 s per GIF.
2. **1 · Fiber.** Each image slice shows its clean anatomy, with all overlays removed by combining every frame. The fiber is pre-placed:
   * *Remembered*: you (or an earlier review) saved it for this exact GIF.
   * *Auto – please check*: found automatically. The tip is found from where tissue heats while the laser is on. Check that the **E** (skull entry) and **T** (tip) handles sit on the fiber.
   * Drag the handles, or press **Place by clicking** and click the entry, then the tip. **Snap to line** fits the line exactly onto the dark fiber.
   * **Save** remembers the fiber for this GIF, keyed by file content, so re-uploading it later starts from your review.
   * **Full screen** (or double-click the image) opens the slice on the whole screen for precise placement:
     * scroll to zoom (up to 16×) and drag empty space to pan; a pixel grid appears when zoomed in;
     * drag a handle, or click it and nudge it with the arrow keys (0.5 px steps, Shift for 2 px);
     * **Stretch to fill** uses the whole screen (pixels are stretched, clicks stay exact). Press `Esc` to go back.
3. **2 · Output.** Choose:
   * **Data:** time and laser status, temperatures, limits, positions, distance to center, distance to tip.
   * **Points:** all, or any subset.
   * **Frames:** all, laser-on only, last frame of each run, or a frame range.
   * **Format:** Excel or CSV.

   Then press **Run**. The app shows the distance table (e.g. `5: 12.3 mm`) and asks whether to download. **Download all results (.zip)** in the sidebar bundles every file you exported.
4. **3 · Visualize.**
   * Play or scrub the GIF with the fiber, center, tip, points and distance labels drawn on top. Toggle each layer, or tick *Original only*.
   * Shortcuts: `Space` plays and pauses; `←`/`→` step through frames.
   * The chart shows each point's temperature over time. Shaded bands mean the laser is on; dashed lines mark a new run. Click the chart to jump to a frame.
   * **Create annotated GIF** exports the overlay as a GIF (2× is sharper, 1× is a smaller file).

### Batch mode (no browser)

```bash
.venv/bin/python batch.py ..                       # every GIF under the parent folder
.venv/bin/python batch.py path/to/case_folder --format csv --frames laser_on
```

Results go to `./visualase_results/` (change with `--out`). Fibers that were found automatically rather than reviewed are flagged in the output.

## How the numbers are read

* **Text:** the Visualase overlay uses a fixed 6×10 pixel font, so every character is matched exactly against bitmap templates rather than guessed by OCR.
* **Status line:** read from the bottom row of the image. A built-in cross-check confirms that watts = 15 W × power %.
* **Point positions:** taken as the center of the magenta point number on the temperature image (±2 px). Labels drawn on top of each other are separated automatically.
* **Points over their limit:** while a reading is at or over its limit, Visualase draws that point's legend line and number in white instead of magenta. Those are read too, and the frame gets an "over its limit" note. If the white "<90" falls on bright white anatomy and can't be seen, the point's limit is taken from the nearest frame where it was readable (also noted).
* **Pixel size:** 1.015625 mm per pixel. That is the DICOM PixelSpacing of the Visualase planning *T1 SE* series (256×256, 260 mm FOV), and the GIF anatomy matches those slices pixel-for-pixel. You can change it in the top-right box or in the Excel *Summary* sheet.
* **Distances:** 2D distances within the slice where the point is drawn.
* **Excel layout:** the workbook keeps distances as formulas. If you edit the fiber coordinates (blue cells) or the pixel size, every distance updates.
* **Half-drawn frames:** some GIF frames are only partially drawn in the source file. Values that cannot be read are left blank, with a note in the *Notes* column.

## Files

```
visualase_app/
  run.sh              start script (creates .venv on first run)
  app.py              local web server (Flask)
  batch.py            command-line batch processing
  core/
    glyphs.py         bitmap font templates + exact matcher
    ocr.py            reads status line, point legend and point labels from a frame
    analysis.py       one pass over a GIF: OCR, clean anatomy per slice, heating map, fiber suggestion
    fiber.py          fiber detection (auto-suggest) and snap-to-line
    export.py         Excel / CSV writers
    render.py         annotated GIF renderer
    recalc.py         optional LibreOffice formula pre-calculation
  static/             the web page (HTML/CSS/JS, no internet needed)
  data/               created on first run, never committed (see .gitignore)
    saved_fibers.json reviewed fibers, remembered per GIF
    uploads/, outputs/  working copies and exported files (safe to delete when the app is stopped)
```
