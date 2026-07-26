#!/usr/bin/env python3
"""XCTID-style fingerprint from verification bytes + SVG animation (no browser).

Port of realasfngl/Grok-Api core/reverse/xctid.py Signature.xs / generate_sign.
Used once HTML verification meta + challenge SVG + x_values are available via HTTP.
"""
from __future__ import annotations

import base64
import hashlib
import math
import random
import re
import struct
import time
from typing import Dict, List

EPOCH = 1682924400


def _h(x: float, param: float, c: float, e: bool):
    f = ((x * (c - param)) / 255.0) + param
    if e:
        return math.floor(f)
    rounded = round(float(f), 2)
    return 0.0 if rounded == 0.0 else rounded


def cubic_bezier_eased(t: float, x1: float, y1: float, x2: float, y2: float) -> float:
    def bezier(u: float):
        omu = 1.0 - u
        b1 = 3.0 * omu * omu * u
        b2 = 3.0 * omu * u * u
        b3 = u * u * u
        return b1 * x1 + b2 * x2 + b3, b1 * y1 + b2 * y2 + b3

    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bezier(mid)[0] < t:
            lo = mid
        else:
            hi = mid
    return bezier(0.5 * (lo + hi))[1]


def xa(svg: str) -> List[List[int]]:
    substr = svg[9:]
    out: List[List[int]] = []
    for part in substr.split("C"):
        cleaned = re.sub(r"[^\d]+", " ", part).strip()
        out.append([0] if cleaned == "" else [int(tok) for tok in cleaned.split() if tok])
    return out


def tohex(num: float) -> str:
    rounded = round(float(num), 2)
    if rounded == 0.0:
        return "0"
    sign = "-" if math.copysign(1.0, rounded) < 0 else ""
    absval = abs(rounded)
    intpart = int(math.floor(absval))
    frac = absval - intpart
    if frac == 0.0:
        return sign + format(intpart, "x")
    frac_digits = []
    f = frac
    for _ in range(20):
        f *= 16
        digit = int(math.floor(f + 1e-12))
        frac_digits.append(format(digit, "x"))
        f -= digit
        if abs(f) < 1e-12:
            break
    frac_str = "".join(frac_digits).rstrip("0")
    return sign + format(intpart, "x") if frac_str == "" else sign + format(intpart, "x") + "." + frac_str


def simulate_style(values: List[int], c: int) -> Dict[str, str]:
    duration = 4096
    current_time = round(c / 10.0) * 10
    t = current_time / duration
    cp = [_h(v, -1 if (i % 2) else 0, 1, False) for i, v in enumerate(values[7:])]
    eased_y = cubic_bezier_eased(t, cp[0], cp[1], cp[2], cp[3])
    start = [float(x) for x in values[0:3]]
    end = [float(x) for x in values[3:6]]
    r = round(start[0] + (end[0] - start[0]) * eased_y)
    g = round(start[1] + (end[1] - start[1]) * eased_y)
    b = round(start[2] + (end[2] - start[2]) * eased_y)
    color = f"rgb({r}, {g}, {b})"
    end_angle = _h(values[6], 60, 360, True)
    angle = end_angle * eased_y
    rad = angle * math.pi / 180.0

    def z(val: float) -> bool:
        return abs(val) < 1e-7

    def integ(val: float) -> bool:
        return abs(val - round(val)) < 1e-7

    cosv, sinv = math.cos(rad), math.sin(rad)
    if z(cosv):
        a = d = 0
    elif integ(cosv):
        a = d = int(round(cosv))
    else:
        a = d = f"{cosv:.6f}"
    if z(sinv):
        bval = cval = 0
    elif integ(sinv):
        bval = int(round(sinv))
        cval = int(round(-sinv))
    else:
        bval = f"{sinv:.7f}"
        cval = f"{(-sinv):.7f}"
    return {"color": color, "transform": f"matrix({a}, {bval}, {cval}, {d}, 0, 0)"}


def fingerprint_from_svg(verification_bytes: bytes, svg: str, x_values: list) -> str:
    arr = list(verification_bytes)
    idx = arr[x_values[0]] % 16
    c = ((arr[x_values[1]] % 16) * (arr[x_values[2]] % 16)) * (arr[x_values[3]] % 16)
    vals = xa(svg)[idx]
    k = simulate_style(vals, c)
    concat = str(k["color"]) + str(k["transform"])
    matches = re.findall(r"[\d.\-]+", concat)
    joined = "".join(tohex(float(m)) for m in matches)
    return joined.replace(".", "").replace("-", "")


def b64d(s: str) -> bytes:
    return base64.b64decode(s + "=" * ((4 - len(s) % 4) % 4))


def generate_statsig(
    method: str,
    path: str,
    verification_b64: str,
    svg: str,
    x_values: list,
    *,
    n: int | None = None,
    rnd: float | None = None,
) -> str:
    n = int(time.time() - EPOCH) if n is None else n
    r = b64d(verification_b64)
    o = fingerprint_from_svg(r, svg, x_values)
    msg = "!".join([method, path, str(n)]) + "obfiowerehiring" + o
    digest = hashlib.sha256(msg.encode()).digest()[:16]
    prefix = int(math.floor((rnd if rnd is not None else random.random()) * 256))
    assembled = bytes([prefix]) + r + struct.pack("<I", n) + digest + bytes([3])
    arr = bytearray(assembled)
    first = arr[0]
    for i in range(1, len(arr)):
        arr[i] ^= first
    return base64.b64encode(bytes(arr)).decode("ascii").rstrip("=")


if __name__ == "__main__":
    # self-check layout only
    meta = b"\x00" * 48
    fp = fingerprint_from_svg(meta, "M0 0C" + " ".join(["1"] * 20), [0, 1, 2, 3])
    print({"fp_len": len(fp), "fp_prefix": fp[:32]})
