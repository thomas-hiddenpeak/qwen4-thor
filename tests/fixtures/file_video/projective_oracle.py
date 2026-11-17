"""Independent exact pixel fixtures; stdlib Fraction + Gauss-Jordan inverse.

Run explicitly to regenerate projective_oracle.json. This is fixture generation,
not a runtime dependency or a reference implementation imported by the tests.
"""

from fractions import Fraction as F
import json
from pathlib import Path

IDENTITY = [65536, 0, 0, 0, 65536, 0, 0, 0, 1 << 30]


def matrix(raw):
    return [[F(raw[3 * r + c], 1 << (30 if c == 2 else 16))
             for c in range(3)] for r in range(3)]


def inverse(m):
    rows = [m[r][:] + [F(r == c) for c in range(3)] for r in range(3)]
    for c in range(3):
        pivot = next(r for r in range(c, 3) if rows[r][c])
        rows[c], rows[pivot] = rows[pivot], rows[c]
        factor = rows[c][c]
        rows[c] = [x / factor for x in rows[c]]
        for r in range(3):
            if r != c:
                factor = rows[r][c]
                rows[r] = [x - factor * y for x, y in zip(rows[r], rows[c])]
    return [row[3:] for row in rows]


def point(p, m):
    q = [sum(p[k] * m[k][c] for k in range(3)) for c in range(3)]
    return q[0] / q[2], q[1] / q[2]


def fixture(name, track, movie):
    width, height = 4, 3
    source = [(i * 37 + c * 71) % 256 for i in range(width * height)
              for c in range(3)]
    t, m = matrix(track), matrix(movie)
    combined = [[sum(t[r][k] * m[k][c] for k in range(3))
                 for c in range(3)] for r in range(3)]
    corners = [point([x, y, 1], combined)
               for x in (0, width) for y in (0, height)]
    xmin, ymin = (min(p[c] for p in corners) for c in (0, 1))
    xmax, ymax = (max(p[c] for p in corners) for c in (0, 1))
    ceil = lambda x: -(-x.numerator // x.denominator)
    out_w, out_h = ceil(xmax - xmin), ceil(ymax - ymin)
    reverse = inverse(combined)
    pixels = []
    for y in range(out_h):
        for x in range(out_w):
            sx, sy = point([xmin + x + F(1, 2), ymin + y + F(1, 2), 1], reverse)
            if not (0 <= sx < width and 0 <= sy < height):
                pixels.extend([0, 0, 0])
                continue
            qx, qy = (int((s * 65536).__floor__()) - 32768 for s in (sx, sy))
            ix, iy = qx // 65536, qy // 65536
            fx, fy = qx % 65536, qy % 65536
            for c in range(3):
                total = 1 << 31
                for dy, wy in enumerate((65536 - fy, fy)):
                    for dx, wx in enumerate((65536 - fx, fx)):
                        px, py = min(max(ix + dx, 0), width - 1), min(max(iy + dy, 0), height - 1)
                        total += source[(py * width + px) * 3 + c] * wx * wy
                pixels.append(total >> 32)
    return dict(name=name, movie=movie, track=track, source=source,
                width=width, height=height, output_width=out_w,
                output_height=out_h, output=pixels,
                min_x=[str(xmin.numerator), str(xmin.denominator)],
                min_y=[str(ymin.numerator), str(ymin.denominator)])


def main():
    scale = IDENTITY[:]
    scale[8] = 1 << 29
    perspective = IDENTITY[:]
    perspective[2], perspective[5] = 1 << 27, 1 << 26
    expanded = perspective[:]
    expanded[2] *= -1
    movie = [49152, 16384, 1 << 26, -8192, 65536, 0, -131072, 65536, 1 << 30]
    cases = [("nonunit_w", scale, IDENTITY),
             ("negative_common_scale", [-x for x in scale], IDENTITY),
             ("two_axis_perspective", perspective, IDENTITY),
             ("near_horizon_expansion", expanded, IDENTITY),
             ("track_then_movie", perspective, movie),
             ("movie_then_track_differs", movie, perspective)]
    output = dict(oracle="Python Fraction; Gauss-Jordan inverse; bilinear16",
                  cases=[fixture(*case) for case in cases])
    Path(__file__).with_suffix(".json").write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    main()
