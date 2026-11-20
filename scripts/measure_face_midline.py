"""Estimate four face landmarks on a supplied portrait and mouth region."""
import argparse
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image


def fit(points):
    """x = a + b*y through the landmarks (the midline is near vertical)."""
    ys = np.array([p[1] for p in points]); xs = np.array([p[0] for p in points])
    b, a = np.polyfit(ys, xs, 1)
    return a, b, [float(x - (a + b * y)) for x, y in points]


def measure(image, region):
    rgb = np.asarray(image.convert('RGB')).astype(float)
    lum, red = rgb.mean(2), rgb[..., 0] - rgb[..., 1]
    cx, cy, rx, ry = region
    x0, x1 = int(cx - rx), int(cx + rx)
    # Lip parting line: darkest pixel per column across the rest mouth.
    stomion = {x: int(np.argmin(lum[int(cy - ry * .5):int(cy + ry * .1), x])) + int(cy - ry * .5) for x in range(x0, x1)}
    sy = float(np.median(list(stomion.values())))
    # Nostrils: darkest 8 % of a window above the mouth, split left/right at the window centre.
    wy0, wy1, wx0, wx1 = int(sy - rx * .55), int(sy - rx * .3), int(cx - rx * .45), int(cx + rx * .2)
    sub = lum[wy0:wy1, wx0:wx1]; mid = (wx1 - wx0) // 2
    nostrils = []
    for part, off in ((sub[:, :mid], 0), (sub[:, mid:], mid)):
        th = np.percentile(part, 8); yy, xx = np.nonzero(part <= th); w = th - part[yy, xx] + 1
        nostrils.append(((xx * w).sum() / w.sum() + wx0 + off, (yy * w).sum() / w.sum() + wy0))
    nose = ((nostrils[0][0] + nostrils[1][0]) / 2, (nostrils[0][1] + nostrils[1][1]) / 2)
    # Upper lip: skin->lip edge per column (largest redness rise above the parting line).
    top = {}
    for x in range(x0, x1):
        a = int(sy - ry * .55); col = np.diff(red[a:int(sy) - 3, x]); top[x] = float(np.argmax(col)) + a + .5
    near = [x for x in top if abs(x - nose[0]) <= rx * .3]
    left = min((x for x in near if x < nose[0]), key=lambda x: (top[x], -x))
    right = min((x for x in near if x > nose[0]), key=lambda x: (top[x], x))
    between = range(left, right + 1); dip = max(top[x] for x in between)
    dips = [x for x in between if top[x] >= dip - .5]
    upper = (float(np.mean(dips)), dip)
    # Lower lip: lip-colour extent per row below the parting line.
    skin = float(np.median(red[int(sy + ry * .8):int(sy + ry * 1.2), int(cx - rx * .3):int(cx + rx * .3)]))
    mids = []
    for y in range(int(sy + 6), int(sy + 22), 2):
        row = red[y, x0:x1]; th = (row.max() + skin) / 2; xs = np.nonzero(row > th)[0] + x0
        if len(xs) and row.max() - skin > 30: mids.append(((xs.min() + xs.max()) / 2, y))
    lower = (float(np.mean([m[0] for m in mids])), float(np.mean([m[1] for m in mids])))
    # Chin: strongest light->dark edge per column below the lip, then symmetric crossings.
    g = np.diff(lum, axis=0); c0, c1 = int(sy + ry * 1.2), int(sy + ry * 2.2)
    contour = {x: float(np.argmin(g[c0:c1, x])) + c0 + .5 for x in range(int(cx - rx * .5), int(cx + rx * .6))}
    xs = np.array(sorted(contour)); ys = np.array([contour[x] for x in xs]); tip = ys.max()
    centres = []
    for h in np.arange(tip - 13, tip - 1, 2.):
        inside = xs[ys >= h]
        if len(inside) and inside[0] > xs[0] and inside[-1] < xs[-1]: centres.append((inside[0] + inside[-1]) / 2)
    chin = (float(np.mean(centres)), float(tip))
    return {'nose': nose, 'upper_lip': upper, 'lower_lip': lower, 'chin': chin}, nostrils


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("portrait", type=Path)
    parser.add_argument("--region", nargs=4, type=float, required=True, metavar=("CX", "CY", "RX", "RY"))
    args = parser.parse_args()
    with Image.open(args.portrait) as image:
        cx, cy, rx, ry = args.region
        if not (rx > 0 and ry > 0 and rx <= cx < image.width-rx and ry <= cy < image.height-ry):
            parser.error("mouth region must be inside the portrait")
        marks, _ = measure(image, args.region)
    order = ("nose", "upper_lip", "lower_lip", "chin")
    a, b, residuals = fit([marks[name] for name in order])
    nx, ny = marks["nose"]; chin_x, chin_y = marks["chin"]
    if chin_y <= ny:parser.error("invalid landmark order")
    slope = (chin_x - nx) / (chin_y - ny)
    lip_offsets = [x - (nx + slope * (y - ny)) for x, y in (marks["upper_lip"], marks["lower_lip"])]
    if not all(math.isfinite(d) and abs(d) <= 1.5 for d in residuals + lip_offsets):
        parser.error("landmarks disagree; inspect the source image")
    print(json.dumps({"face_midline": {n: [round(x, 1) for x in marks[n]] for n in order},
                      "intercept": a, "slope": b, "residuals_px": residuals}, indent=2))


if __name__ == "__main__":
    main()
