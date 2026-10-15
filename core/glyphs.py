"""Bitmap templates for the Visualase overlay font and an exact template matcher.

Every character is drawn in a fixed 6-px-wide cell: 5 columns x 10 rows
(row 9 is the descender row used by '.' and ':'). Because the GIF text is
pixel-perfect, exact template matching is more reliable than general OCR.
"""
import numpy as np
from scipy import ndimage

_RAW = {
    # digits (note: zero is diamond-shaped in this font)
    '0': ["..#..", ".#.#.", "#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    '1': ["..#..", ".##..", "#.#..", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"],
    '2': [".###.", "#...#", "#...#", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
    '3': ["#####", "....#", "...#.", "..#..", ".###.", "....#", "....#", "#...#", ".###."],
    '4': ["...#.", "...#.", "..##.", ".#.#.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    '5': ["#####", "#....", "#....", "#.##.", "##..#", "....#", "....#", "#...#", ".###."],
    '6': [".###.", "#...#", "#....", "#....", "####.", "#...#", "#...#", "#...#", ".###."],
    '7': ["#####", "....#", "...#.", "...#.", "..#..", "..#..", ".#...", ".#...", ".#..."],
    '8': [".###.", "#...#", "#...#", "#...#", ".###.", "#...#", "#...#", "#...#", ".###."],
    '9': [".###.", "#...#", "#...#", "#...#", ".####", "....#", "....#", "#...#", ".###."],
    # punctuation
    '<': ["....#", "...#.", "..#..", ".#...", "#....", ".#...", "..#..", "...#.", "....#"],
    '-': [".....", ".....", ".....", ".....", "#####", ".....", ".....", ".....", "....."],
    '.': [".....", ".....", ".....", ".....", ".....", ".....", ".....", "..#..", ".###.", "..#.."],
    ':': [".....", ".....", "..#..", ".###.", "..#..", ".....", ".....", "..#..", ".###.", "..#.."],
    '=': [".....", ".....", ".....", "#####", ".....", ".....", "#####", ".....", "....."],
    '%': [".#..#", "#.#.#", ".#.#.", "...#.", "..#..", ".#...", ".#.#.", "#.#.#", "#..#."],
    # letters used by the status line ("Laser off." / "Laser Power = 75%  11.2 Watts")
    'L': ["#....", "#....", "#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    'a': [".....", ".....", ".....", ".###.", "....#", ".####", "#...#", "#..##", ".##.#"],
    's': [".....", ".....", ".....", ".###.", "#...#", ".##..", "...#.", "#...#", ".###."],
    'e': [".....", ".....", ".....", ".###.", "#...#", "#####", "#....", "#...#", ".###."],
    'r': [".....", ".....", ".....", "#.##.", "##..#", "#....", "#....", "#....", "#...."],
    'o': [".....", ".....", ".....", ".###.", "#...#", "#...#", "#...#", "#...#", ".###."],
    'f': ["..##.", ".#..#", ".#...", ".#...", "####.", ".#...", ".#...", ".#...", ".#..."],
    't': [".....", ".#...", ".#...", "####.", ".#...", ".#...", ".#...", ".#..#", "..##."],
    'P': ["####.", "#...#", "#...#", "#...#", "####.", "#....", "#....", "#....", "#...."],
    'w': [".....", ".....", ".....", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."],
    'W': ["#...#", "#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", "#.#.#", ".#.#."],
}


def _to_arr(rows):
    rows = list(rows) + ["....."] * (10 - len(rows))
    return np.array([[c == '#' for c in r] for r in rows], dtype=bool)


TEMPLATES = {k: _to_arr(v) for k, v in _RAW.items()}
BLANK = np.zeros((10, 5), dtype=bool)
DIGITS = '0123456789'
LEGEND_CHARS = '0123456789<.-'          # magenta point legend + point labels
STATUS_CHARS = '0123456789:.=%LasePowerftW'


def find_glyphs(mask, chars=LEGEND_CHARS, strict=True):
    """Locate glyphs whose 10x5 cell matches `mask` exactly.

    Returns a list of (y, x, char) with (y, x) the cell's top-left pixel.
    strict=True also requires blank pixels in a 1-px margin above, left and right
    of the cell, so glyphs that touch other drawing are not matched.
    """
    H, W = mask.shape
    pad = np.zeros((H + 12, W + 7), dtype=np.int32)
    pad[1:H + 1, 1:W + 1] = mask          # image (y, x) -> pad (y+1, x+1)
    box = ndimage.correlate(pad, np.ones((11, 7), dtype=np.int32), mode='constant', origin=(-5, -3))
    out = []
    for ch in chars:
        t = TEMPLATES[ch]
        k = np.zeros((11, 7), dtype=np.int32)
        k[1:11, 1:6] = t
        n = int(t.sum())
        hits = ndimage.correlate(pad, k, mode='constant', origin=(-5, -3))
        ok = hits == n
        if strict:
            ok &= box == n
        ys, xs = np.nonzero(ok[:H + 1, :W + 1])
        out.extend((int(y), int(x), ch) for y, x in zip(ys, xs))
    return out
