"""
Manual check of the land area of ONE Fairfax County parcel.

Compares:
  1) the recorded land area in the tax table ("legal" table: ACRES / SQFT)
  2) the area computed from the official parcel shape (Parcels layer)

Usage:
    python check_parcel_area.py
    python check_parcel_area.py "0502 10F 0001"

Requires only:  pip install requests
"""
import sys
import requests

PARCELS = "https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/services/Parcels/FeatureServer/0"
LEGAL = "https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/services/OpenData_A7/FeatureServer/1"

# EPSG 2283 = NAD83 / Virginia North (US feet). The server reprojects the data,
# so coordinates arrive in feet and the area comes out in square feet.
OUT_SR = 2283
SQFT_PER_ACRE = 43_560


def query(url, where, fields="*", geometry=False):
    params = {
        "where": where,
        "outFields": fields,
        "returnGeometry": "true" if geometry else "false",
        "f": "json",
    }
    if geometry:
        params["outSR"] = OUT_SR
    r = requests.get(f"{url}/query", params=params, timeout=60)
    r.raise_for_status()
    js = r.json()
    if "error" in js:
        raise RuntimeError(js["error"])
    return js.get("features", [])


def ring_area(ring):
    """Shoelace formula: planar area of a ring of (x, y) vertices."""
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += x1 * y2 - x2 * y1
    return s / 2.0


def point_in_ring(pt, ring):
    """Ray casting: is the point inside the ring?"""
    x, y = pt
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def polygon_area(rings):
    """Holes are found by nesting (a ring inside an odd number of rings), not by ring direction.
    Same rule as agentic_pilot.py."""
    areas = [abs(ring_area(r)) for r in rings]
    total = 0.0
    for i, r in enumerate(rings):
        depth = sum(1 for j, o in enumerate(rings) if j != i and areas[j] > areas[i] and point_in_ring(r[0], o))
        total += areas[i] if depth % 2 == 0 else -areas[i]
    return abs(total)


def main():
    parid = sys.argv[1] if len(sys.argv) > 1 else "0191 01  0003"
    compact = "".join(parid.split())

    # 1) Tax table
    legal = query(LEGAL, f"PARID = '{parid}'",
                  "PARID,TAXYR,ACRES,SQFT,LEGAL1,LEGAL2,ADRNO,ADRSTR")
    print("=== 1) Tax table (legal) ===")
    if not legal:
        print("No record found. Check the spaces in the PARID (e.g. '0191 01  0003').")
        return
    latest = max(legal, key=lambda f: f["attributes"].get("TAXYR") or 0)["attributes"]
    for k, v in latest.items():
        print(f"  {k:7}: {v}")
    if latest.get("SQFT"):
        recorded = float(latest["SQFT"])
        src = "SQFT"
    elif latest.get("ACRES"):
        recorded = float(latest["ACRES"]) * SQFT_PER_ACRE
        src = "ACRES x 43.560"
    else:
        recorded, src = None, "no area"
    if recorded:
        print(f"  -> recorded land area: {recorded:,.0f} sq ft ({recorded / SQFT_PER_ACRE:.2f} acres)  [source: {src}]")

    # 2) Official parcel shape (the PIN may be stored with or without spaces)
    variants = {parid, " ".join(parid.split()), compact}
    where = "PIN IN (" + ",".join(f"'{v}'" for v in variants) + ")"
    feats = query(PARCELS, where, "PIN", geometry=True)
    print("\n=== 2) Official parcel shape (Parcels layer) ===")
    if not feats:
        print("No shape found for", variants)
        return
    total = 0.0
    for i, f in enumerate(feats, 1):
        rings = f["geometry"]["rings"]
        a = polygon_area(rings)
        total += a
        print(f"  Shape {i}: PIN={f['attributes']['PIN']!r}, "
              f"{len(rings)} ring(s), {sum(len(r) for r in rings)} vertices, "
              f"area {a:,.0f} sq ft")
        for j, r in enumerate(rings, 1):
            print(f"     ring {j}: {ring_area(r):+,.0f} sq ft  (sign shows ring direction only)")
    print(f"  -> computed area: {total:,.0f} sq ft ({total / SQFT_PER_ACRE:.2f} acres)")

    # 3) Comparison
    if recorded:
        diff = (total - recorded) / recorded
        print("\n=== 3) Comparison ===")
        print(f"  Shape / record = {total / recorded:.2f}x   (difference {diff:+.0%})")


if __name__ == "__main__":
    main()
