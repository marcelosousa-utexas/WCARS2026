#!/usr/bin/env python3
"""
Agentic pilot - Multi-Agent Continuous Auditing of Public Real Property
WCARS 75 (Rutgers, Nov 2026) - Marcelo M. S. Sousa

Two agents on US open data (Washington, DC and Fairfax County, VA):

  Agent 1 (LLM)            - reads only field METADATA (names, aliases, types) and
                             non-personal category lists, and maps each jurisdiction's
                             schema to one common audit schema. Output goes to a
                             review file: a human must approve each mapping.
  Agent 2 (deterministic)  - uses only the APPROVED mapping. Downloads public parcels,
                             links tax roll <-> polygons by key, computes land area from
                             geometry and flags exceptions. No LLM involved.

Every step is written to outputs/audit_trail.jsonl (the audit trail).

Usage
  python agentic_pilot.py agent1                 # LLM mapping -> outputs/review/*.csv
  (open the review CSVs, set approved = Y / N)
  python agentic_pilot.py agent2 --max-parcels 1000
  python agentic_pilot.py agent1 --llm reference # no LLM: use the reference mapping (for testing)
  python agentic_pilot.py profile                # Agent 1: what can each dataset prove? -> review CSV

Dependencies: requests, pandas (+ your LLM connector). matplotlib is optional (maps).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# 1. Configuration: public endpoints (ArcGIS REST) and the common audit schema
# ---------------------------------------------------------------------------
DC_LAND = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Property_and_Land/MapServer/61"
DC_ITSPE = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Property_and_Land/MapServer/53"
FFX_PARCELS = "https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/services/Parcels/FeatureServer/0"
FFX_PARCEL_DATA = "https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/services/OpenData_A6/FeatureServer/1"
FFX_VALUES = "https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/services/OpenData_A6/FeatureServer/2"
FFX_LAND = "https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/services/OpenData_A6/FeatureServer/3"
FFX_LEGAL = "https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/services/OpenData_A7/FeatureServer/1"
DC_PARCELS = "https://maps2.dcgis.dc.gov/dcgis/rest/services/DCGIS_DATA/Property_and_Land/MapServer/40"

# Layer roles are defined by the auditor (human). Field meanings are proposed by Agent 1.
JURISDICTIONS = {
    "DC": {
        "label": "Washington, DC",
        "out_wkid": 2248,  # NAD83 / Maryland (ftUS) -> areas in square feet
        "layers": {
            "geometry": {"url": DC_LAND, "role": "Polygons of property related to the District government, with attributes"},
            "tax_roll": {"url": DC_ITSPE, "role": "Tax assessment roll (table, no geometry)"},
        },
        "category_field": None,
        # Audit scope, set by the auditor: the entity whose financial statements are audited.
        "reporting_entity": "District of Columbia (the government of the District)",
        # Extra layers read only by the dataset profile (step "profile").
        "profile_extra_layers": {
            "parcels_all": {"url": DC_PARCELS,
                            "role": "Parcel shapes for the whole District, with tax and survey attributes"},
        },
    },
    "FAIRFAX": {
        "label": "Fairfax County, VA",
        "out_wkid": 2283,  # NAD83 / Virginia North (ftUS) -> areas in square feet
        "layers": {
            "geometry": {"url": FFX_PARCELS, "role": "Parcel polygons for the whole county"},
            "parcel_data": {"url": FFX_PARCEL_DATA, "role": "Tax table: land use, zoning (one-to-many)"},
            "legal": {"url": FFX_LEGAL, "role": "Tax table: legal description, deeded land area (one-to-one)"},
            "values": {"url": FFX_VALUES, "role": "Tax table: assessed values (one-to-one)"},
            "land": {"url": FFX_LAND, "role": "Tax table: land lines in sq ft / acres (one-to-many)"},
        },
        # Fairfax does not publish owner names: public land must be found by land-use category.
        "category_field": ("parcel_data", "LUC_DESC"),
        "reporting_entity": "Fairfax County, Virginia (the county government)",
        "profile_extra_layers": {},
    },
}

AUDIT_SCHEMA = {
    "parcel_key_geometry": "Identifier of the parcel in the polygon (geometry) layer",
    "parcel_key_tax": "Identifier of the parcel in the tax / assessment tables",
    "owner_name": "Name of the property owner",
    "land_use": "Land-use or property-use description or code",
    "recorded_land_area": "Land area recorded in the roll or deed (NOT computed from geometry)",
    "assessed_land_value": "Current assessed value of the land only",
    "assessed_total_value": "Current assessed value, land plus buildings",
    "site_address": "Street address of the property",
    "deed_reference": "Deed book/page or instrument number",
    "tax_year": "Tax year of the record",
}

# Values of these fields are never sent to the LLM (privacy by design).
PERSONAL_FIELD_PATTERN = re.compile(r"OWN|NAME|ADDR|CAREOF|MORTG|STREET|PREMISE|CITYST", re.I)

OUT = "outputs"
AREA_TOLERANCE = 0.10  # 10 % divergence between recorded area and geometry area ...
AREA_MIN_ABS_SQFT = 1000  # ... AND at least 1,000 sq ft (~93 m2): ignores slivers and rounding of tiny lots


# ---------------------------------------------------------------------------
# 2. Audit trail
# ---------------------------------------------------------------------------
def log(agent, step, **detail):
    os.makedirs(OUT, exist_ok=True)
    rec = {"time": dt.datetime.now().isoformat(timespec="seconds"), "agent": agent, "step": step, **detail}
    with open(os.path.join(OUT, "audit_trail.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    print(f"[{agent}] {step}: " + ", ".join(f"{k}={v}" for k, v in detail.items() if not isinstance(v, (list, dict))))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 3. ArcGIS REST helpers (deterministic)
# ---------------------------------------------------------------------------
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "WCARS-agentic-pilot/0.1 (research)"


def _call(url, params, method="POST", tries=3):
    params = {**params, "f": "json"}
    for attempt in range(tries):
        try:
            r = SESSION.post(url, data=params, timeout=90) if method == "POST" else SESSION.get(url, params=params, timeout=90)
            r.raise_for_status()
            js = r.json()
            if "error" in js:
                raise RuntimeError(f"ArcGIS error at {url}: {js['error']}")
            return js
        except Exception as e:  # noqa: BLE001
            if attempt == tries - 1:
                raise
            print(f"  retry {attempt + 1} after error: {e}")
            time.sleep(2 * (attempt + 1))


def layer_metadata(url):
    js = _call(url, {}, method="GET")
    return {
        "name": js.get("name"),
        "type": js.get("type"),
        "geometryType": js.get("geometryType"),
        "description": re.sub(r"<[^>]+>", " ", js.get("description") or "").strip()[:1500],
        "fields": [{"name": f["name"], "alias": f.get("alias"), "type": f.get("type", "").replace("esriFieldType", ""),
                    "length": f.get("length"),
                    "codes": [c.get("name") for c in ((f.get("domain") or {}).get("codedValues") or [])][:30] or None}
                   for f in js.get("fields", [])],
        "maxRecordCount": js.get("maxRecordCount", 1000),
        "supportsStatistics": (js.get("advancedQueryCapabilities") or {}).get("supportsStatistics", True),
    }


def distinct_values(url, field, where="1=1", limit=300):
    base = {"where": where, "outFields": field, "returnDistinctValues": "true", "returnGeometry": "false"}
    try:
        js = _call(f"{url}/query", {**base, "orderByFields": field})
    except Exception:  # noqa: BLE001 - some services reject orderByFields with distinct
        js = _call(f"{url}/query", base)
    vals = [ft["attributes"].get(field) for ft in js.get("features", [])]
    return [v for v in vals if v not in (None, "")][:limit]


def query_all(url, where="1=1", out_fields="*", geometry=False, out_wkid=None, chunk=500):
    """Fetch every matching record using objectIds (works on MapServer, FeatureServer and tables)."""
    ids = _call(f"{url}/query", {"where": where, "returnIdsOnly": "true"}).get("objectIds") or []
    ids = sorted(ids)
    rows = []
    for i in range(0, len(ids), chunk):
        params = {"objectIds": ",".join(map(str, ids[i:i + chunk])), "outFields": out_fields,
                  "returnGeometry": "true" if geometry else "false"}
        if geometry and out_wkid:
            params["outSR"] = out_wkid
        js = _call(f"{url}/query", params)
        for ft in js.get("features", []):
            row = dict(ft.get("attributes", {}))
            if geometry:
                row["_rings"] = (ft.get("geometry") or {}).get("rings")
            rows.append(row)
    return rows


def query_in(url, field, values, extra_where=None, out_fields="*", geometry=False, out_wkid=None, batch=150):
    """WHERE field IN (...) in batches."""
    rows = []
    vals = [v for v in dict.fromkeys(values) if v not in (None, "")]
    for i in range(0, len(vals), batch):
        quoted = ",".join("'" + str(v).replace("'", "''") + "'" for v in vals[i:i + batch])
        where = f"{field} IN ({quoted})" + (f" AND ({extra_where})" if extra_where else "")
        rows += query_all(url, where=where, out_fields=out_fields, geometry=geometry, out_wkid=out_wkid)
    return rows


def _ring_signed_area(ring):
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _point_in_ring(pt, ring):
    """Ray casting: is point pt strictly inside the ring?"""
    x, y = pt
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def polygon_area(rings):
    """Planar area of an Esri polygon. Units = units of the projected CRS.

    Holes are found by NESTING (a ring inside an odd number of other rings is a hole),
    not by ring direction. Some published polygons have rings in the wrong direction;
    summing signed areas then makes parts cancel out (validation of 4 Nov 2026,
    DC PAR 01210028: 12,299 sq ft computed vs 11,677,105 in DC's own CALCULATEDAREA).
    """
    rings = [r for r in (rings or []) if len(r) >= 3]
    if not rings:
        return None
    if len(rings) == 1:
        return abs(_ring_signed_area(rings[0]))
    areas = [abs(_ring_signed_area(r)) for r in rings]
    total = 0.0
    for i, r in enumerate(rings):
        depth = sum(1 for j, other in enumerate(rings)
                    if j != i and areas[j] > areas[i] and _point_in_ring(r[0], other))
        total += areas[i] if depth % 2 == 0 else -areas[i]
    return abs(total)


def norm_key(v):
    return re.sub(r"[^0-9A-Za-z]", "", str(v)).upper() if v not in (None, "") else None


def reformat_like(value, template):
    """Rewrite '0391010001' as '0391 01 0001' if the template has that layout."""
    a = norm_key(value)
    slots = [i for i, c in enumerate(template) if c.isalnum()]
    if a is None or len(a) != len(slots):
        return value
    out = list(template)
    for ch, i in zip(a, slots):
        out[i] = ch
    return "".join(out)


def to_num(v):
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 4. LLM connector (one place to swap the model)
# ---------------------------------------------------------------------------
LLM_MODEL = os.environ.get("LLM_MODEL", "openai:gpt-4.1")  # "provider:model"; change with --model or LLM_MODEL


def get_llm():
    """Returns a LangChain chat model. Provider-neutral: use any model you prefer.

    Option 1 (default): LangChain's init_chat_model with "provider:model",
        e.g. "openai:gpt-4.1", "anthropic:claude-sonnet-5-5", "google_genai:gemini-2.5-pro".
        Install the provider package (e.g. pip install langchain-openai) and set its API key
        as an environment variable (e.g. OPENAI_API_KEY). Never write keys in this file.
    Option 2: create a local file llm_local.py (not shared, listed in .gitignore) with a function
        get_llm(model) that returns any LangChain chat model, e.g. an institutional gateway.
    """
    try:
        import llm_local  # optional, private connector
        return llm_local.get_llm(LLM_MODEL)
    except ImportError:
        pass
    from langchain.chat_models import init_chat_model
    try:
        return init_chat_model(LLM_MODEL, temperature=0.0)
    except TypeError:  # some models do not accept temperature
        return init_chat_model(LLM_MODEL)


def call_llm(system, user):
    from langchain_core.messages import HumanMessage, SystemMessage
    llm = get_llm()
    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    return resp.content if isinstance(resp.content, str) else json.dumps(resp.content)


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


# ---------------------------------------------------------------------------
# 5. AGENT 1 - semantic schema translation (LLM, metadata only)
# ---------------------------------------------------------------------------
SYSTEM_A1 = (
    "You are Agent 1 of a continuous-auditing pipeline for public real property. "
    "You map heterogeneous county GIS and tax-roll schemas to one common audit schema. "
    "You only see field metadata, never property records. Be conservative: if no field "
    "fits a target, say so. Return strict JSON only."
)


def build_a1_prompt(jkey, meta, categories):
    payload = {
        "jurisdiction": JURISDICTIONS[jkey]["label"],
        "target_audit_schema": AUDIT_SCHEMA,
        "layers": {lk: {"role": JURISDICTIONS[jkey]["layers"][lk]["role"], "fields": m["fields"]}
                   for lk, m in meta.items()},
    }
    task = (
        "Task 1: For each target field, list the source field(s) that carry that meaning, per layer. "
        "Use the layer roles and the field names/aliases. Note the unit when relevant (e.g., sq ft, acres, USD). "
        "Give a confidence between 0 and 1 and a one-sentence rationale.\n"
    )
    out_spec = {
        "mappings": [{"layer": "<layer key>", "source_field": "<field name>", "target_field": "<audit schema key>",
                      "unit": "<unit or null>", "confidence": 0.0, "rationale": "<one sentence>"}],
        "unmapped_targets": ["<audit schema keys with no source field>"],
    }
    if categories:
        task += (
            "Task 2: The county does not publish owner names. From the list of land-use categories below, "
            "select those that most likely indicate GOVERNMENT-OWNED land (county, state, federal, schools, parks, "
            "public facilities, exempt public). Give confidence and rationale for each.\n"
        )
        payload["land_use_categories"] = categories
        out_spec["public_land_use_values"] = [{"value": "<exact category>", "confidence": 0.0, "rationale": "<...>"}]
    return task + "Return JSON with exactly this structure:\n" + json.dumps(out_spec, indent=1) + \
        "\n\nINPUT:\n" + json.dumps(payload, indent=1, ensure_ascii=False)


def reference_answer(jkey, meta, categories):
    """Offline reference (prepared with Claude, Anthropic) - used only with --llm reference."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference_mapping_claude.json")
    ref = json.load(open(path, encoding="utf-8"))[jkey]
    if categories:
        kw = re.compile(r"PUBLIC|COUNTY|GOVERN|FEDERAL|STATE|SCHOOL|PARK|MUNICIPAL|EXEMPT", re.I)
        ref["public_land_use_values"] = [{"value": c, "confidence": 0.5, "rationale": "keyword heuristic (offline mode)"}
                                         for c in categories if kw.search(str(c))]
    return ref


def guard_mappings(jkey, answer, meta):
    """Deterministic check of the LLM answer against the real metadata."""
    by_key = {lk.lower(): lk for lk in meta}
    by_name = {str(m.get("name", "")).lower(): lk for lk, m in meta.items()}
    fields = {lk: {f["name"].upper(): f["name"] for f in m["fields"]} for lk, m in meta.items()}
    items = answer if isinstance(answer, list) else (answer.get("mappings") or answer.get("mapping") or [])
    rows, rejected = [], []
    for m in items:
        if not isinstance(m, dict):
            continue
        lay = str(m.get("layer") or m.get("source_layer") or "").strip().lower()
        fld = str(m.get("source_field") or m.get("field") or "").strip()
        tgt = str(m.get("target_field") or m.get("target") or "").strip().lower()
        lk = by_key.get(lay) or by_name.get(lay)
        if not lk:  # layer not recognised: accept only if the field exists in exactly one layer
            hits = [k for k, fs in fields.items() if fld.upper() in fs]
            lk = hits[0] if len(hits) == 1 else None
        reason = None
        if not lk:
            reason = "unknown layer"
        elif fld.upper() not in fields[lk]:
            reason = "field does not exist in layer"
        elif tgt not in AUDIT_SCHEMA:
            reason = "unknown target field"
        if reason:
            rejected.append({**m, "reason": reason})
            continue
        rows.append({"jurisdiction": jkey, "layer": lk, "source_field": fields[lk][fld.upper()], "target_field": tgt,
                     "unit": m.get("unit"), "confidence": m.get("confidence"), "rationale": m.get("rationale"),
                     "approved": ""})
    return rows, rejected


def agent1(args):
    os.makedirs(os.path.join(OUT, "review"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "llm"), exist_ok=True)
    for jkey in args.jurisdictions:
        cfg = JURISDICTIONS[jkey]
        meta = {lk: layer_metadata(l["url"]) for lk, l in cfg["layers"].items()}
        n_fields = sum(len(m["fields"]) for m in meta.values())
        log("agent1", "metadata_read", jurisdiction=jkey, layers=len(meta), fields=n_fields)
        categories = None
        if cfg["category_field"]:
            lk, fld = cfg["category_field"]
            categories = distinct_values(cfg["layers"][lk]["url"], fld)
            log("agent1", "categories_read", jurisdiction=jkey, field=fld, n=len(categories))
        # privacy check: no values of personal fields are ever sent
        prompt = build_a1_prompt(jkey, meta, categories)
        t0 = time.time()
        if args.llm == "reference":
            answer, raw = reference_answer(jkey, meta, categories), "(reference mapping, no LLM call)"
        else:
            raw = call_llm(SYSTEM_A1, prompt)
            answer = parse_json(raw)
        secs = round(time.time() - t0, 1)
        with open(os.path.join(OUT, "llm", f"{jkey}_prompt.txt"), "w", encoding="utf-8") as f:
            f.write(SYSTEM_A1 + "\n\n" + prompt)
        with open(os.path.join(OUT, "llm", f"{jkey}_response.txt"), "w", encoding="utf-8") as f:
            f.write(raw)
        # deterministic guard: keep only mappings to fields that really exist (hallucination check).
        # Tolerant to formatting: layer given by key or by layer name, field names in any case.
        rows, rejected = guard_mappings(jkey, answer, meta)
        cols = ["jurisdiction", "layer", "source_field", "target_field", "unit", "confidence", "rationale", "approved"]
        mpath = os.path.join(OUT, "review", f"{jkey}_mapping_review.csv")
        if os.path.exists(mpath) and read_review(mpath)["approved"].str.strip().ne("").any():
            mpath = mpath.replace(".csv", f"_proposed_{dt.datetime.now():%Y%m%d_%H%M%S}.csv")
            print(f"  An approved mapping already exists: the new proposal is saved as {mpath}")
        pd.DataFrame(rows, columns=cols).to_csv(mpath, index=False)
        pd.DataFrame(rejected, columns=sorted({k for r in rejected for k in r}) or ["layer", "source_field", "target_field", "reason"]).to_csv(os.path.join(OUT, "review", f"{jkey}_rejected_by_guard.csv"), index=False)
        if categories:
            cat_norm = {str(c).strip().upper(): c for c in categories}
            cats = []
            for c in answer.get("public_land_use_values") or answer.get("public_land_use") or []:
                v = c.get("value") if isinstance(c, dict) else c
                if str(v).strip().upper() in cat_norm:
                    cc = c if isinstance(c, dict) else {}
                    cats.append({"value": cat_norm[str(v).strip().upper()], "confidence": cc.get("confidence"),
                                 "rationale": cc.get("rationale"), "approved": ""})
            cpath = os.path.join(OUT, "review", f"{jkey}_public_land_use_review.csv")
            if os.path.exists(cpath) and read_review(cpath)["approved"].str.strip().ne("").any():
                cpath = cpath.replace(".csv", f"_proposed_{dt.datetime.now():%Y%m%d_%H%M%S}.csv")
                print(f"  Approved categories already exist: the new proposal is saved as {cpath}")
            pd.DataFrame(cats, columns=["value", "confidence", "rationale", "approved"]).to_csv(cpath, index=False)
        log("agent1", "mapping_proposed", jurisdiction=jkey, model=args.llm if args.llm == "reference" else LLM_MODEL,
            seconds=secs, mappings=len(rows), rejected_by_guard=len(rejected), prompt_sha=sha(prompt),
            unmapped=answer.get("unmapped_targets"))
        if not rows:
            print(f"  WARNING: no valid mapping for {jkey}. Check outputs/llm/{jkey}_response.txt "
                  f"and outputs/review/{jkey}_rejected_by_guard.csv")
    print(f"\nNext: open {OUT}/review/*.csv, set approved = Y or N for each row, save, then run agent2.")


# ---------------------------------------------------------------------------
# 5b. AGENT 1 - dataset profile: what can each dataset prove? (step "profile")
#
#   Step 2 (code, no LLM): read each layer's description, fields and most frequent values.
#   Step 3 (LLM):          answer two questions per layer, with evidence and confidence:
#                          Q1 are the records land parcels, buildings/facilities, or mixed?
#                          Q2 does the layer show the owner, and which values are the reporting entity?
#   Step 4 (code, no LLM): check the answer against the data and compute its impact
#                          (how many records are kept or moved to an information list).
#   The auditor then approves or corrects each answer in outputs/review/<J>_dataset_profile_review.csv
# ---------------------------------------------------------------------------
# Data minimization: a value is sent to the LLM only if it appears in at least K_MIN records.
# Governments own many parcels, so their names pass; values that identify one person do not.
# This follows the "minimum cell size" rule of statistical disclosure control. It is a parameter.
K_MIN = 20
TOP_VALUES = 30       # most frequent values kept per field in the profile
TOP_SHORT = 8         # values sent to the LLM for fields with many distinct values (codes, addresses, ids)
CATEGORICAL_MAX = 150 # a field with at most this many distinct values is a category list: send up to TOP_VALUES
OWNER_FIELD_HINT = re.compile(r"OWN", re.I)

Q1 = "Are the records land parcels, buildings or facilities, or mixed?"
Q2 = "Does the dataset show the owner? Which values are the reporting entity?"


def _sql_list(values):
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in values)


def _eq(field, value):
    return f"{field} = '" + str(value).replace("'", "''") + "'"


def count_where(url, where="1=1"):
    try:
        return int(_call(f"{url}/query", {"where": where, "returnCountOnly": "true"}).get("count", 0))
    except Exception as e:  # noqa: BLE001
        print(f"  count failed ({where[:60]}...): {e}")
        return None


def value_counts(url, field):
    """Server-side GROUP BY: most frequent values of one field, with counts (no records downloaded)."""
    stats = json.dumps([{"statisticType": "count", "onStatisticField": field, "outStatisticFieldName": "n_records"}])
    base = {"where": "1=1", "groupByFieldsForStatistics": field, "outStatistics": stats, "returnGeometry": "false"}
    try:
        js = _call(f"{url}/query", {**base, "orderByFields": "n_records DESC", "resultRecordCount": 200}, tries=1)
        partial = False
    except Exception:  # noqa: BLE001 - some services reject ordering of statistics
        js = _call(f"{url}/query", base, tries=1)
        partial = True
    out = []
    for ft in js.get("features", []):
        a = ft.get("attributes", {})
        n = a.get("n_records") or a.get("N_RECORDS")
        v = a.get(field)
        if v is None:
            v = next((val for k, val in a.items() if k.upper() == field.upper()), None)
        if v not in (None, "") and n:
            out.append((str(v).strip(), int(n)))
    out.sort(key=lambda t: -t[1])
    return out, partial


def profile_layer(url, role):
    """Step 2 - deterministic profile of one layer. Only aggregated, frequent values are kept."""
    meta = layer_metadata(url)
    prof = {"name": meta["name"], "role": role, "description": meta["description"],
            "has_shapes": bool(meta.get("geometryType")), "geometry_type": meta.get("geometryType"),
            "records_total": count_where(url), "fields": []}
    for f in meta["fields"]:
        entry = {"name": f["name"], "alias": f["alias"], "type": f["type"]}
        if f.get("codes"):
            entry["codes"] = f["codes"]
        if f["type"] == "String" and meta.get("supportsStatistics", True) and (f.get("length") or 0) <= 255:
            try:
                vc, partial = value_counts(url, f["name"])
                frequent = [{"value": v, "records": n} for v, n in vc if n >= K_MIN][:TOP_VALUES]
                entry["frequent_values"] = frequent
                entry["n_values_seen"] = len(vc)
                if partial:
                    entry["note"] = "value list may be incomplete"
            except Exception as e:  # noqa: BLE001
                entry["note"] = f"value counts not available ({str(e)[:80]})"
        prof["fields"].append(entry)
    return prof


SYSTEM_A1P = (
    "You are Agent 1 of a continuous-auditing pipeline for public LAND. "
    "You receive a profile of each dataset: its official description, its fields and, for some fields, "
    "the most frequent values with record counts. You never see individual property records. "
    "Answer two questions for each dataset, citing the evidence you used. "
    "If the profile does not allow an answer, say cannot_determine: never guess. Return strict JSON only."
)


def compact_profile(profiles, max_len=80):
    """Shorter view of the profile for the LLM (fewer tokens, same information for the two questions)."""
    out = {}
    for dk, p in profiles.items():
        fields = []
        for f in p["fields"]:
            e = {"name": f["name"]}
            if f.get("alias") and f["alias"] != f["name"]:
                e["alias"] = f["alias"]
            e["type"] = f["type"]
            if f.get("codes"):
                e["codes"] = f["codes"][:TOP_SHORT]
            fv = f.get("frequent_values") or []
            if fv:
                top = TOP_VALUES if (f.get("n_values_seen") or 0) <= CATEGORICAL_MAX else TOP_SHORT
                e["values"] = [[str(x["value"])[:max_len], x["records"]] for x in fv[:top]]
                if len(fv) > top:
                    e["more_values_not_shown"] = len(fv) - top
            fields.append(e)
        out[dk] = {k: p[k] for k in ("name", "role", "description", "has_shapes", "records_total")} | {"fields": fields}
    return out


def build_profile_prompt(jkey, profiles):
    entity = JURISDICTIONS[jkey]["reporting_entity"]
    task = (
        f"Audit scope (set by the auditor): land owned by the reporting entity '{entity}'. "
        "Only land is audited: buildings are out of scope.\n"
        f"Q1 for each dataset with shapes: {Q1} The question is about what each SHAPE represents: "
        "the boundary of a land parcel, or the outline or marker of a building or facility "
        "(for example a building footprint, or a small box drawn around a point). "
        "A land parcel with a building on it is still a land parcel: the use of the land or of its building "
        "(housing, office, school) does not make the record a building record. Look for fields that tell where "
        "the shape came from or what kind of shape it is. If mixed, give that field and the exact values that "
        "mark building or facility records. For datasets without shapes, answer no_shapes.\n"
        f"Q2 for each dataset: {Q2} Give the owner field and the exact values that are the reporting entity. "
        "List separately (related_values) values that may be agencies or authorities linked to the entity "
        "but whose inclusion the auditor must decide, and other governments (other_government_values). "
        "Use only values that appear in the profile.\n"
        "Evidence: quote the description text or the values and counts you used. Confidence between 0 and 1.\n"
    )
    out_spec = {"datasets": [{
        "dataset": "<dataset key>",
        "record_type": {"answer": "land_parcels | buildings_or_facilities | mixed | no_shapes | cannot_determine",
                        "field": "<field or null>", "building_values": ["<exact values>"],
                        "evidence": "<...>", "confidence": 0.0},
        "owner": {"answer": "yes | no | cannot_determine", "field": "<field or null>",
                  "entity_values": ["<exact values>"], "related_values": ["<exact values>"],
                  "other_government_values": ["<exact values>"], "evidence": "<...>", "confidence": 0.0},
    }]}
    return task + "Return JSON with exactly this structure:\n" + json.dumps(out_spec, indent=1) + \
        "\n\nDATASET PROFILES (values are [value, record count]):\n" + \
        json.dumps(compact_profile(profiles), separators=(",", ":"), ensure_ascii=False)


def reference_profile_answer(jkey, profiles):
    """Offline heuristic (no LLM), used only with --llm reference. Deliberately simple."""
    words = [w for w in re.findall(r"[A-Z]+", JURISDICTIONS[jkey]["reporting_entity"].split("(")[0].upper())
             if w not in {"OF", "THE", "AND"}]
    bkw = re.compile(r"\b(PTS|POINTS?|BUILDINGS?|FACILIT(Y|IES)|STRUCTURES?)\b", re.I)
    out = []
    for dk, p in profiles.items():
        rt = {"answer": "no_shapes" if not p["has_shapes"] else "land_parcels", "field": None,
              "building_values": [], "evidence": "offline heuristic", "confidence": 0.3}
        ow = {"answer": "no", "field": None, "entity_values": [], "related_values": [],
              "other_government_values": [], "evidence": "offline heuristic: no field named like OWNER",
              "confidence": 0.3}
        for f in p["fields"]:
            vals = [fv["value"] for fv in f.get("frequent_values", [])]
            if p["has_shapes"] and re.search(r"SOURCE|TYPE|CLASS", f["name"], re.I):
                b = [v for v in vals if bkw.search(v)]
                if b:
                    rt = {"answer": "mixed", "field": f["name"], "building_values": b,
                          "evidence": f"offline heuristic: values {b} in {f['name']}", "confidence": 0.3}
            if ow["field"] is None and re.fullmatch(r"OWNER_?NAME|OWNNAME|OWNER", f["name"], re.I):
                ent = [v for v in vals if all(w in v.upper() for w in words) and len(v) <= len(" ".join(words)) + 4]
                rel = [v for v in vals if all(w in v.upper() for w in words) and v not in ent]
                ow = {"answer": "yes", "field": f["name"], "entity_values": ent, "related_values": rel,
                      "other_government_values": [v for v in vals if "UNITED STATES" in v.upper()],
                      "evidence": f"offline heuristic: owner field {f['name']}", "confidence": 0.3}
        out.append({"dataset": dk, "record_type": rt, "owner": ow})
    return {"datasets": out}


def _as_list(v):
    if v in (None, "", []):
        return []
    return [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]


def guard_profile(jkey, answer, profiles, urls):
    """Step 4 - deterministic check of the LLM diagnosis and its impact on the data."""
    rows = []
    items = {str(d.get("dataset", "")).strip(): d for d in (answer.get("datasets") or []) if isinstance(d, dict)}
    for dk, p in profiles.items():
        url, d = urls[dk], items.get(dk, {})
        fields = {f["name"].upper(): f["name"] for f in p["fields"]}
        total = p["records_total"]

        # ---- Q1: land parcels, buildings, or mixed?
        rt = d.get("record_type") or {}
        ans = str(rt.get("answer") or "cannot_determine").strip().lower()
        fld, vals = str(rt.get("field") or "").strip(), _as_list(rt.get("building_values"))
        status, note, kept, moved = "ok", "", total, 0
        if not p["has_shapes"]:
            if ans != "no_shapes":
                note = f"agent said '{ans}', but the dataset has no shapes: set to no_shapes"
            ans, status = "no_shapes", "ok"
        elif ans == "mixed":
            if fld.upper() not in fields:
                status, note = "rejected", f"field '{fld}' does not exist"
            elif not vals:
                status, note = "rejected", "no values given to separate building records"
            else:
                fld = fields[fld.upper()]
                moved = count_where(url, f"{fld} IN ({_sql_list(vals)})")
                if not moved:
                    status, note = "rejected", "the proposed values match no records"
                else:
                    kept = total - moved if total is not None else None
        elif ans not in ("land_parcels", "buildings_or_facilities", "cannot_determine"):
            status, note = "rejected", f"unknown answer '{ans}'"
        rows.append({"jurisdiction": jkey, "dataset": dk, "dataset_name": p["name"], "question": Q1,
                     "proposed_answer": ans, "field": fld or "", "values": "; ".join(vals),
                     "evidence": rt.get("evidence", ""), "confidence": rt.get("confidence"),
                     "records_total": total, "records_kept": kept, "records_moved_to_info_list": moved,
                     "guard_status": status, "guard_note": note, "approved": "", "auditor_note": ""})

        # ---- Q2: owner and reporting entity
        ow = d.get("owner") or {}
        ans = str(ow.get("answer") or "cannot_determine").strip().lower()
        flds = [x.strip() for x in re.split(r"[;,]", str(ow.get("field") or "")) if x.strip()]
        ent, rel, oth = (_as_list(ow.get(k)) for k in ("entity_values", "related_values", "other_government_values"))
        status, note, kept, moved, owner_where = "ok", "", total, 0, None
        if ans == "yes":
            bad = [x for x in flds if x.upper() not in fields]
            if not flds or bad:
                status, note = "rejected", f"field(s) {bad or flds} do not exist"
            elif not ent:
                status, note = "rejected", "no values given for the reporting entity"
            else:
                flds = [fields[x.upper()] for x in flds]

                def any_field(vals):  # a value may appear in any of the owner fields (e.g. owner 1 or owner 2)
                    return "(" + " OR ".join(f"{x} IN ({_sql_list(vals)})" for x in flds) + ")"

                missing = [v for v in ent if not count_where(url, any_field([v]))]
                if missing:
                    note = f"values not found in data, removed: {missing}. "
                    ent = [v for v in ent if v not in missing]
                if not ent:
                    status = "rejected"
                else:
                    owner_where = any_field(ent)
                    kept = count_where(url, owner_where)
                    moved = total - kept if (total is not None and kept is not None) else None
                    if len(flds) > 1:
                        note += f"main field {flds[0]} alone: {count_where(url, f'{flds[0]} IN ({_sql_list(ent)})')}. "
                    if rel:
                        note += "related values (not included unless the auditor adds them): " + \
                                ", ".join(f"{v} ({count_where(url, any_field([v]))})" for v in rel[:8]) + "."
        elif ans in ("no", "cannot_determine"):
            hints = [f["name"] for f in p["fields"] if OWNER_FIELD_HINT.search(f["name"])]
            if hints:
                status, note = "check", f"fields that look like an owner field exist: {hints}"
        else:
            status, note = "rejected", f"unknown answer '{ans}'"
        fld = "; ".join(flds)
        rows.append({"jurisdiction": jkey, "dataset": dk, "dataset_name": p["name"], "question": Q2,
                     "proposed_answer": ans, "field": fld or "", "values": "; ".join(ent),
                     "evidence": ow.get("evidence", "") + (f" | other governments: {'; '.join(oth)}" if oth else ""),
                     "confidence": ow.get("confidence"), "records_total": total,
                     "records_kept": kept, "records_moved_to_info_list": moved,
                     "guard_status": status, "guard_note": note.strip(), "approved": "", "auditor_note": "",
                     "_owner_where": owner_where})

        # ---- combined impact on a dataset with shapes, when both filters are usable
        r1, r2 = rows[-2], rows[-1]
        if (p["has_shapes"] and r1["proposed_answer"] == "mixed" and r1["guard_status"] == "ok"
                and r2["proposed_answer"] == "yes" and r2["guard_status"] == "ok"):
            w = (f"{r2['_owner_where']} AND "
                 f"({r1['field']} IS NULL OR {r1['field']} NOT IN ({_sql_list(r1['values'].split('; '))}))")
            r2["guard_note"] = (r2["guard_note"] + f" Both filters together: {count_where(url, w)} land parcels "
                                f"of the reporting entity.").strip()
    return rows


def agent1_profile(args):
    """Steps 2-4: profile, diagnosis, check. Does not touch the mapping review files."""
    os.makedirs(os.path.join(OUT, "review"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "llm"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "profile"), exist_ok=True)
    for jkey in args.jurisdictions:
        cfg = JURISDICTIONS[jkey]
        layers = {**cfg["layers"], **cfg.get("profile_extra_layers", {})}
        urls = {dk: l["url"] for dk, l in layers.items()}
        log("agent1", "profile_started", jurisdiction=jkey, reporting_entity=cfg["reporting_entity"],
            datasets=list(layers))
        saved = os.path.join(OUT, "profile", f"{jkey}_profile.json")
        profiles = {}
        if getattr(args, "reuse_profile", False) and os.path.exists(saved):
            profiles = json.load(open(saved, encoding="utf-8"))
            log("agent1", "profile_reused", jurisdiction=jkey, file=saved)
        for dk, l in layers.items():
            if dk in profiles:
                continue
            print(f"  profiling {jkey}/{dk} ...")
            t0 = time.time()
            profiles[dk] = profile_layer(l["url"], l["role"])
            log("agent1", "dataset_profiled", jurisdiction=jkey, dataset=dk, name=profiles[dk]["name"],
                records=profiles[dk]["records_total"], fields=len(profiles[dk]["fields"]),
                seconds=round(time.time() - t0, 1))
        with open(os.path.join(OUT, "profile", f"{jkey}_profile.json"), "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=1, ensure_ascii=False)
        prompt = build_profile_prompt(jkey, profiles)
        t0 = time.time()
        if args.llm == "reference":
            answer, raw = reference_profile_answer(jkey, profiles), "(offline heuristic, no LLM call)"
        else:
            raw = call_llm(SYSTEM_A1P, prompt)
            answer = parse_json(raw)
        secs = round(time.time() - t0, 1)
        with open(os.path.join(OUT, "llm", f"{jkey}_profile_prompt.txt"), "w", encoding="utf-8") as f:
            f.write(SYSTEM_A1P + "\n\n" + prompt)
        with open(os.path.join(OUT, "llm", f"{jkey}_profile_response.txt"), "w", encoding="utf-8") as f:
            f.write(raw)
        rows = guard_profile(jkey, answer, profiles, urls)
        cols = ["jurisdiction", "dataset", "dataset_name", "question", "proposed_answer", "field", "values",
                "evidence", "confidence", "records_total", "records_kept", "records_moved_to_info_list",
                "guard_status", "guard_note", "approved", "auditor_note"]
        path = os.path.join(OUT, "review", f"{jkey}_dataset_profile_review.csv")
        if os.path.exists(path) and read_review(path)["approved"].str.strip().ne("").any():
            path = path.replace(".csv", f"_proposed_{dt.datetime.now():%Y%m%d_%H%M%S}.csv")
            print(f"  An approved review already exists: the new proposal is saved as {path}")
        df = pd.DataFrame(rows, columns=cols).replace(r"[\r\n]+", " ", regex=True)
        df.to_csv(path, index=False)  # standard CSV (comma, UTF-8); read_review() also accepts ";"
        log("agent1", "profile_proposed", jurisdiction=jkey, model=args.llm if args.llm == "reference" else LLM_MODEL,
            seconds=secs, answers=len(rows), prompt_sha=sha(prompt), prompt_chars=len(prompt),
            rejected_by_guard=sum(r["guard_status"] == "rejected" for r in rows))
    print(f"\nNext: open {OUT}/review/*_dataset_profile_review.csv. For each row, set approved = Y or N, "
          "or write a correction in auditor_note.")


# ---------------------------------------------------------------------------
# 6. Human-in-the-loop gate
# ---------------------------------------------------------------------------
def read_review(path):
    """Reads a review CSV saved by Excel in any locale (comma or semicolon; UTF-8 or Windows-1252)."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return pd.read_csv(path, dtype=str, sep=None, engine="python", encoding=enc).fillna("")
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"Could not read {path}")


def approved_mapping(jkey):
    path = os.path.join(OUT, "review", f"{jkey}_mapping_review.csv")
    df = read_review(path)
    ok = df[df["approved"].str.strip().str.upper().isin(["Y", "YES", "S", "SIM"])]
    if ok.empty:
        sys.exit(f"No approved rows in {path}. A human must approve the mapping before Agent 2 runs.")
    m = {}
    for _, r in ok.iterrows():
        m.setdefault(r["target_field"], []).append((r["layer"], r["source_field"]))
    log("hitl", "mapping_approved", jurisdiction=jkey, approved=len(ok), rejected=len(df) - len(ok))
    return m


def pick(m, target, layer):
    for lk, f in m.get(target, []):
        if lk == layer:
            return f
    return None


def approved_categories(jkey):
    path = os.path.join(OUT, "review", f"{jkey}_public_land_use_review.csv")
    df = read_review(path)
    vals = df[df["approved"].str.strip().str.upper().isin(["Y", "YES", "S", "SIM"])]["value"].tolist()
    if not vals:
        sys.exit(f"No approved land-use categories in {path}.")
    log("hitl", "categories_approved", jurisdiction=jkey, approved=len(vals))
    return vals


# ---------------------------------------------------------------------------
# 7. AGENT 2 - deterministic integrity tests (no LLM)
# ---------------------------------------------------------------------------
def area_flag(rec_area, geo_area):
    if rec_area in (None, 0) or geo_area in (None, 0):
        return None
    return abs(geo_area - rec_area) / rec_area


def agent2_dc(m, max_parcels):
    cfg = JURISDICTIONS["DC"]
    kg = pick(m, "parcel_key_geometry", "geometry")
    kt = pick(m, "parcel_key_tax", "tax_roll")
    area_g = pick(m, "recorded_land_area", "geometry")
    area_t = pick(m, "recorded_land_area", "tax_roll")
    val_t = pick(m, "assessed_land_value", "tax_roll")
    use_g = pick(m, "land_use", "geometry")
    if not kg or not kt:
        sys.exit("DC: approved mapping needs parcel_key_geometry (geometry) and parcel_key_tax (tax_roll).")
    # Context fields shown to the auditor (not used in any test): where the record comes from and who owns it.
    # Validation of 4 Nov 2026: layer 61 mixes parcel boundaries with small facility boxes
    # (e.g. SOURCE "CHARTER SCHOOL PTS" inside a large federal parcel).
    ctx = ["SOURCE", "OWNERNAME", "LEASED", "SITENAME"]
    fields = ",".join(f for f in {kg, area_g, use_g, "OBJECTID", "CALCULATEDAREA", *ctx} if f)
    t0 = time.time()
    geo = query_all(cfg["layers"]["geometry"]["url"], out_fields=fields, geometry=True, out_wkid=cfg["out_wkid"])
    if max_parcels:
        geo = geo[:max_parcels]
    log("agent2", "polygons_downloaded", jurisdiction="DC", n=len(geo))
    keys = [g.get(kg) for g in geo]
    tfields = ",".join(f for f in {kt, area_t, val_t} if f)
    tax = query_in(cfg["layers"]["tax_roll"]["url"], kt, keys, out_fields=tfields)
    log("agent2", "tax_roll_downloaded", jurisdiction="DC", n=len(tax))
    tax_by = {}
    for t in tax:
        tax_by.setdefault(norm_key(t.get(kt)), []).append(t)
    # One SSL can be drawn as SEVERAL polygons (e.g. one park split by facility): aggregate polygons per key
    # before comparing with the single recorded area (validation of 3 Nov 2026 found this case).
    valid_ssl = re.compile(r"^(\d{4}[A-Z]{0,2}\s+\d{4}|PAR \d{8}|RES [0-9A-Z]{8})$")
    groups = {}
    for g in geo:
        k = g.get(kg)
        nk = norm_key(k) or f"__nokey_{g.get('OBJECTID')}"
        grp = groups.setdefault(nk, {"key": k, "area": 0.0, "n": 0, "use": g.get(use_g) if use_g else None,
                                     "rec_g": to_num(g.get(area_g)) if area_g else None, "dc_calc": 0.0})
        grp["area"] += polygon_area(g.get("_rings")) or 0
        grp["n"] += 1
        grp["dc_calc"] += to_num(g.get("CALCULATEDAREA")) or 0
        for c in ctx:
            vals = grp.setdefault("ctx_" + c, [])
            v = g.get(c)
            if v not in (None, "") and v not in vals:
                vals.append(str(v).strip())
    rows = []
    for nk, grp in groups.items():
        k = grp["key"]
        ks = str(k or "").strip().upper()
        is_valid = bool(valid_ssl.match(ks))
        is_res = ks.startswith("RES ")
        trs = tax_by.get(nk, []) if is_valid else []
        rec_t = sum(to_num(t.get(area_t)) or 0 for t in trs) if (trs and area_t) else None
        geo_area = grp["area"] or None
        rows.append({
            "key": k, "land_use": grp["use"],
            "area_geometry_sqft": round(geo_area, 1) if geo_area else None,
            "area_recorded_geometry_layer_sqft": grp["rec_g"], "area_recorded_tax_roll_sqft": rec_t,
            "assessed_land_value": sum(to_num(t.get(val_t)) or 0 for t in trs) if (trs and val_t) else None,
            "source": " | ".join(grp.get("ctx_SOURCE", [])) or None,
            "owner_name": " | ".join(grp.get("ctx_OWNERNAME", [])) or None,
            "leased": " | ".join(grp.get("ctx_LEASED", [])) or None,
            "site_name": " | ".join(grp.get("ctx_SITENAME", [])) or None,
            "area_dc_calculated_sqft": grp["dc_calc"] or None,  # DC's own area computed from the shape
            # Self-check of OUR method: our area vs DC's own computed area (should agree within 2%)
            "info_method_check": bool(grp["dc_calc"] and geo_area
                                      and abs(geo_area - grp["dc_calc"]) / grp["dc_calc"] > 0.02),
            "n_polygons": grp["n"], "n_tax_records": len(trs),
            "flag_missing_key": not ks,
            "flag_placeholder_key": bool(ks) and not is_valid,  # e.g. "N/A", "FREEWAY ROW", "0000 Unassessed"
            "flag_no_geometry": geo_area in (None, 0),
            # DC "RES" keys are federal reservations: by design they have no tax lot -> separate, expected category
            "flag_not_in_tax_roll": is_valid and not is_res and not trs,
            "info_reservation_no_tax_lot": is_res and not trs,
            "info_multi_polygon_key": grp["n"] > 1,
            "flag_one_to_many": len(trs) > 1,
            "div_geometry_vs_tax_roll": area_flag(rec_t, geo_area),
        })
    df = pd.DataFrame(rows)
    diff = (pd.to_numeric(df["area_geometry_sqft"], errors="coerce")
            - pd.to_numeric(df["area_recorded_tax_roll_sqft"], errors="coerce")).abs()
    df["flag_area_divergence"] = (df["div_geometry_vs_tax_roll"].fillna(0) > AREA_TOLERANCE) & (diff.fillna(0) >= AREA_MIN_ABS_SQFT)
    return df, round(time.time() - t0, 1), geo


# ---------------------------------------------------------------------------
# 7b. AGENT 2 using the APPROVED dataset profile (scope: land of the reporting entity)
# ---------------------------------------------------------------------------
YES = {"Y", "YES", "S", "SIM"}


def approved_profile(jkey):
    """Reads <J>_dataset_profile_review.csv. Returns {dataset: {"record_type": {...}, "owner": {...}}}
    with only the rows a human approved, or None if there is no profile review."""
    path = os.path.join(OUT, "review", f"{jkey}_dataset_profile_review.csv")
    if not os.path.exists(path):
        return None
    df = read_review(path)
    prof, n_ok = {}, 0
    for _, r in df.iterrows():
        if r["approved"].strip().upper() not in YES:
            continue
        n_ok += 1
        q = "owner" if r["question"].startswith("Does the dataset show the owner") else "record_type"
        prof.setdefault(r["dataset"], {})[q] = {
            "answer": r["proposed_answer"].strip().lower(),
            "fields": [x.strip() for x in re.split(r"[;,]", r["field"]) if x.strip()],
            "values": [x.strip() for x in r["values"].split(";") if x.strip()],
        }
    log("hitl", "dataset_profile_approved", jurisdiction=jkey, approved=n_ok, rejected=len(df) - n_ok)
    return prof


def _layer_url(jkey, dk):
    cfg = JURISDICTIONS[jkey]
    return {**cfg["layers"], **cfg.get("profile_extra_layers", {})}[dk]["url"]


def _match_field(meta, name):
    """Field of a layer with the same name (any case), or None."""
    for f in meta["fields"]:
        if name and f["name"].upper() == name.upper():
            return f["name"]
    return None


def agent2_dc_scoped(m, prof, max_parcels):
    """DC with the approved profile:
    1) list layer: keep records of the reporting entity, move building/facility records to an info list;
    2) land shapes: taken from a dataset approved as 'land_parcels';
    3) recorded land area: tax roll. Same tests as before."""
    cfg = JURISDICTIONS["DC"]
    kg = pick(m, "parcel_key_geometry", "geometry")
    kt = pick(m, "parcel_key_tax", "tax_roll")
    area_g = pick(m, "recorded_land_area", "geometry")
    area_t = pick(m, "recorded_land_area", "tax_roll")
    val_t = pick(m, "assessed_land_value", "tax_roll")
    use_g = pick(m, "land_use", "geometry")
    if not kg or not kt:
        sys.exit("DC: approved mapping needs parcel_key_geometry (geometry) and parcel_key_tax (tax_roll).")
    lp = prof.get("geometry", {})
    own, rt = lp.get("owner", {}), lp.get("record_type", {})
    if own.get("answer") != "yes" or not own.get("fields") or not own.get("values"):
        sys.exit("DC: the profile review has no approved owner answer for the list layer (geometry).")
    own_f, ent = own["fields"], set(own["values"])
    src_f = rt["fields"][0] if rt.get("answer") == "mixed" and rt.get("fields") else None
    bvals = set(rt.get("values") or []) if src_f else set()
    # where do land shapes come from? the list layer if approved as land parcels, else another approved one
    if rt.get("answer") == "land_parcels":
        shape_dk = "geometry"
    else:
        shape_dk = next((dk for dk, a in prof.items() if dk != "geometry"
                         and (a.get("record_type") or {}).get("answer") == "land_parcels"), None)
    if not shape_dk:
        sys.exit("DC: no dataset approved as 'land_parcels' to supply land shapes.")
    shape_url = _layer_url("DC", shape_dk)
    smeta = layer_metadata(shape_url)
    ks = _match_field(smeta, kg)
    calc_f = _match_field(smeta, "CALCULATEDAREA")
    if not ks:
        sys.exit(f"DC: the shape dataset '{shape_dk}' has no field '{kg}' to link parcels.")
    log("agent2", "scope_applied", jurisdiction="DC", owner_fields=own_f, entity_values=sorted(ent),
        building_field=src_f, building_values=sorted(bvals), shape_dataset=shape_dk)

    t0 = time.time()
    ctx = [c for c in ("SITENAME", "LEASED") if _match_field(layer_metadata(cfg["layers"]["geometry"]["url"]), c)]
    lfields = ",".join(dict.fromkeys(f for f in [kg, area_g, use_g, "OBJECTID", *own_f, src_f, *ctx] if f))
    recs = query_all(cfg["layers"]["geometry"]["url"], out_fields=lfields, geometry=False)
    log("agent2", "list_layer_downloaded", jurisdiction="DC", n=len(recs))
    scope, info = [], []
    for r in recs:
        owners = [str(r.get(f) or "").strip() for f in own_f]
        is_ent = any(o in ent for o in owners)
        is_bld = bool(src_f) and str(r.get(src_f) or "").strip() in bvals
        if is_ent and not is_bld:
            scope.append(r)
        else:
            info.append({"key": r.get(kg), "owner": " | ".join(o for o in owners if o) or None,
                         "source": r.get(src_f) if src_f else None, "site_name": r.get("SITENAME"),
                         "land_use": r.get(use_g) if use_g else None,
                         "reason": "; ".join(x for x, c in (("building_or_facility_record", is_bld),
                                                            ("not_reporting_entity", not is_ent)) if c)})
    log("agent2", "scope_split", jurisdiction="DC", in_scope_records=len(scope), info_list=len(info),
        buildings=sum("building" in i["reason"] for i in info),
        not_entity=sum("not_reporting_entity" in i["reason"] for i in info))

    valid_ssl = re.compile(r"^(\d{4}[A-Z]{0,2}\s+\d{4}|PAR \d{8}|RES [0-9A-Z]{8})$")
    groups = {}
    for r in scope:
        k = r.get(kg)
        nk = norm_key(k) or f"__nokey_{r.get('OBJECTID')}"
        g = groups.setdefault(nk, {"key": k, "n_list": 0, "use": r.get(use_g) if use_g else None,
                                   "rec_list": to_num(r.get(area_g)) if area_g else None,
                                   "site": r.get("SITENAME")})
        g["n_list"] += 1
    keys = [g["key"] for g in groups.values() if valid_ssl.match(str(g["key"] or "").strip().upper())]
    if max_parcels:
        keys = keys[:max_parcels]
        groups = {nk: g for nk, g in groups.items() if g["key"] in set(keys) or not valid_ssl.match(str(g["key"] or "").strip().upper())}
    shapes = query_in(shape_url, ks, keys, out_fields=",".join(f for f in (ks, calc_f) if f),
                      geometry=True, out_wkid=cfg["out_wkid"])
    log("agent2", "land_shapes_downloaded", jurisdiction="DC", dataset=shape_dk, n=len(shapes))
    shp_by = {}
    for s_ in shapes:
        shp_by.setdefault(norm_key(s_.get(ks)), []).append(s_)
    tfields = ",".join(f for f in {kt, area_t, val_t} if f)
    tax = query_in(cfg["layers"]["tax_roll"]["url"], kt, keys, out_fields=tfields)
    log("agent2", "tax_roll_downloaded", jurisdiction="DC", n=len(tax))
    tax_by = {}
    for t in tax:
        tax_by.setdefault(norm_key(t.get(kt)), []).append(t)

    rows = []
    for nk, g in groups.items():
        k = g["key"]
        kstr = str(k or "").strip().upper()
        is_valid = bool(valid_ssl.match(kstr))
        is_res = kstr.startswith("RES ")
        shp = shp_by.get(nk, []) if is_valid else []
        trs = tax_by.get(nk, []) if is_valid else []
        geo_area = sum(polygon_area(s_.get("_rings")) or 0 for s_ in shp) or None
        dc_calc = sum(to_num(s_.get(calc_f)) or 0 for s_ in shp) if calc_f else 0
        rec_t = sum(to_num(t.get(area_t)) or 0 for t in trs) if (trs and area_t) else None
        rows.append({
            "key": k, "land_use": g["use"], "site_name": g["site"],
            "area_geometry_sqft": round(geo_area, 1) if geo_area else None,
            "area_recorded_list_layer_sqft": g["rec_list"], "area_recorded_tax_roll_sqft": rec_t,
            "assessed_land_value": sum(to_num(t.get(val_t)) or 0 for t in trs) if (trs and val_t) else None,
            "area_dc_calculated_sqft": dc_calc or None,
            "info_method_check": bool(dc_calc and geo_area and abs(geo_area - dc_calc) / dc_calc > 0.02),
            "n_list_records": g["n_list"], "n_polygons": len(shp), "n_tax_records": len(trs),
            "flag_missing_key": not kstr,
            "flag_placeholder_key": bool(kstr) and not is_valid,
            "flag_no_land_shape": is_valid and not shp,
            "flag_not_in_tax_roll": is_valid and not is_res and not trs,
            "info_reservation_no_tax_lot": is_res and not trs,
            "info_multi_polygon_key": len(shp) > 1,
            "flag_one_to_many": len(trs) > 1,
            "div_geometry_vs_tax_roll": area_flag(rec_t, geo_area),
        })
    df = pd.DataFrame(rows)
    diff = (pd.to_numeric(df["area_geometry_sqft"], errors="coerce")
            - pd.to_numeric(df["area_recorded_tax_roll_sqft"], errors="coerce")).abs()
    df["flag_area_divergence"] = (df["div_geometry_vs_tax_roll"].fillna(0) > AREA_TOLERANCE) & (diff.fillna(0) >= AREA_MIN_ABS_SQFT)
    scope_info = {"reporting_entity": cfg["reporting_entity"], "list_layer_records": len(recs),
                  "in_scope_records": len(scope), "moved_buildings_or_facilities": sum("building" in i["reason"] for i in info),
                  "moved_not_reporting_entity": sum("not_reporting_entity" in i["reason"] for i in info),
                  "land_shapes_from": smeta.get("name") or shape_dk,
                  "scope_note": "Land owned by the reporting entity, as approved by the auditor in the dataset profile."}
    return df, round(time.time() - t0, 1), shapes, [s_.get(ks) for s_ in shapes], pd.DataFrame(info), scope_info


def agent2_fairfax(m, cats, max_parcels):
    cfg = JURISDICTIONS["FAIRFAX"]
    L = cfg["layers"]
    kg = pick(m, "parcel_key_geometry", "geometry")
    kt = pick(m, "parcel_key_tax", "parcel_data") or "PARID"
    use = pick(m, "land_use", "parcel_data") or "LUC_DESC"
    yr = pick(m, "tax_year", "parcel_data") or "TAXYR"
    area_fields = [f for lk, f in m.get("recorded_land_area", []) if lk == "legal"]  # all approved
    area_fields.sort(key=lambda f: "ACRE" in f.upper())  # exact sq ft first; acres (rounded) only as fallback
    area_legal = area_fields[0] if area_fields else None
    val = pick(m, "assessed_land_value", "values")
    if not kg:
        sys.exit("FAIRFAX: approved mapping needs parcel_key_geometry (geometry).")
    t0 = time.time()
    years = [int(y) for y in distinct_values(L["parcel_data"]["url"], yr) if str(y).isdigit()]
    cur = max(years) if years else None
    ywhere = f"{yr}={cur}" if cur else None
    pdata = query_in(L["parcel_data"]["url"], use, cats, extra_where=ywhere, out_fields=f"{kt},{use},{yr}")
    parids = list(dict.fromkeys(p.get(kt) for p in pdata if p.get(kt)))
    if max_parcels:
        parids = parids[:max_parcels]
    use_by = {}
    for p in pdata:
        use_by.setdefault(p.get(kt), p.get(use))
    log("agent2", "public_parcels_selected", jurisdiction="FAIRFAX", tax_year=cur, n=len(parids))
    kl = pick(m, "parcel_key_tax", "legal") or kt
    kv = pick(m, "parcel_key_tax", "values") or kt
    # Each tax table may be published up to a different tax year: take, per parcel, the LATEST year available.
    legal = query_in(L["legal"]["url"], kl, parids,
                     out_fields=",".join(f for f in {kl, yr, *area_fields} if f)) if area_fields else []
    values = query_in(L["values"]["url"], kv, parids,
                      out_fields=",".join(f for f in {kv, val, yr} if f)) if val else []

    def latest(rows, key):
        best = {}
        for r in rows:
            k = norm_key(r.get(key))
            if k not in best or (to_num(r.get(yr)) or 0) > (to_num(best[k].get(yr)) or 0):
                best[k] = r
        return best

    legal_last, values_last = latest(legal, kl), latest(values, kv)
    def sqft(r):
        """First non-zero approved area field, converted to sq ft (acres x 43,560)."""
        for f in area_fields:
            v = to_num(r.get(f)) or 0
            if v > 0:
                return v * (43560.0 if "ACRE" in f.upper() else 1.0), f
        return None, None

    legal_by, area_source = {}, {}
    for k, r in legal_last.items():
        legal_by[k], area_source[k] = sqft(r)
    src_counts = pd.Series(list(area_source.values())).fillna("none").value_counts().to_dict()
    log("agent2", "recorded_area_source", jurisdiction="FAIRFAX", approved_fields=area_fields, used=src_counts)
    val_by = {k: to_num(r.get(val)) for k, r in values_last.items()}
    yrs_l = sorted({r.get(yr) for r in legal_last.values()})
    yrs_v = sorted({r.get(yr) for r in values_last.values()})
    log("agent2", "tax_tables_read", jurisdiction="FAIRFAX", legal_years=yrs_l, values_years=yrs_v,
        legal_n=len(legal_by), values_n=len(val_by))
    # key-format check: is PARID written like PIN?
    sample_pin = _call(f"{L['geometry']['url']}/query", {"where": f"{kg} IS NOT NULL", "outFields": kg,
                                                          "resultRecordCount": 1, "returnGeometry": "false"}
                       ).get("features", [])
    template = sample_pin[0]["attributes"][kg] if sample_pin else None
    query_keys = [reformat_like(p, template) if template else p for p in parids]
    reformatted = sum(1 for a, b in zip(parids, query_keys) if a != b)
    log("agent2", "key_format_check", jurisdiction="FAIRFAX", pin_example=template, reformatted=reformatted)
    geo = query_in(L["geometry"]["url"], kg, query_keys, out_fields=kg, geometry=True, out_wkid=cfg["out_wkid"])
    log("agent2", "polygons_downloaded", jurisdiction="FAIRFAX", n=len(geo))
    geo_by = {}
    for g in geo:
        geo_by.setdefault(norm_key(g.get(kg)), []).append(g)
    rows = []
    for p in parids:
        nk = norm_key(p)
        gs = geo_by.get(nk, [])
        geo_area = sum(polygon_area(g.get("_rings")) or 0 for g in gs) if gs else None
        rec = legal_by.get(nk)
        rows.append({
            "key": p, "land_use": use_by.get(p),
            "area_geometry_sqft": round(geo_area, 1) if geo_area else None,
            "area_recorded_tax_roll_sqft": rec, "assessed_land_value": val_by.get(nk),
            "n_polygons": len(gs),
            "flag_no_polygon": not gs,
            "flag_multi_polygon": len(gs) > 1,
            "flag_missing_recorded_area": rec in (None, 0),
            "div_geometry_vs_tax_roll": area_flag(rec, geo_area),
        })
    df = pd.DataFrame(rows)
    diff = (pd.to_numeric(df["area_geometry_sqft"], errors="coerce")
            - pd.to_numeric(df["area_recorded_tax_roll_sqft"], errors="coerce")).abs()
    df["flag_area_divergence"] = (df["div_geometry_vs_tax_roll"].fillna(0) > AREA_TOLERANCE) & (diff.fillna(0) >= AREA_MIN_ABS_SQFT)
    return df, round(time.time() - t0, 1), geo


def summarize(jkey, df, secs):
    flags = [c for c in df.columns if c.startswith("flag_")]
    s = {"jurisdiction": JURISDICTIONS[jkey]["label"], "parcels_tested": int(len(df)), "seconds": secs}
    for c in flags + [c for c in df.columns if c.startswith("info_")]:
        s[c] = int(df[c].fillna(False).astype(bool).sum())
    s["parcels_with_any_flag"] = int(df[flags].fillna(False).astype(bool).any(axis=1).sum())
    s["median_abs_area_divergence"] = (round(float(df["div_geometry_vs_tax_roll"].dropna().median()), 4)
                                       if df["div_geometry_vs_tax_roll"].notna().any() else None)
    s["assessed_land_value_total"] = float(pd.to_numeric(df["assessed_land_value"], errors="coerce").sum())
    return s


def quick_map(jkey, df, geo, key_field_values):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Polygon as MplPoly
    except ImportError:
        log("agent2", "map_skipped", jurisdiction=jkey,
            reason="matplotlib is not installed in this Python environment (pip install matplotlib)")
        return
    flagged = set(df.loc[df["flag_area_divergence"], "key"].map(norm_key))
    fig, ax = plt.subplots(figsize=(8, 8))
    for g, k in zip(geo, key_field_values):
        for ring in g.get("_rings") or []:
            ax.add_patch(MplPoly(ring, closed=True, lw=0.3,
                                 fc="#C0563A" if norm_key(k) in flagged else "#6FA287",
                                 ec="#1F4E45", alpha=0.85))
    ax.autoscale_view()
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(f"{JURISDICTIONS[jkey]['label']}: public land parcels (red = area divergence > {int(AREA_TOLERANCE*100)}% and > {AREA_MIN_ABS_SQFT:,} sq ft)")
    fig.savefig(os.path.join(OUT, f"{jkey}_map.png"), dpi=200, bbox_inches="tight")
    plt.close(fig)
    log("agent2", "map_saved", jurisdiction=jkey, file=os.path.join(OUT, f"{jkey}_map.png"))


def agent2(args):
    summaries = []
    for jkey in args.jurisdictions:
        m = approved_mapping(jkey)
        prof = approved_profile(jkey)
        info, scope_info = None, {}
        kg = pick(m, "parcel_key_geometry", "geometry")
        if jkey == "DC":
            if prof:
                df, secs, geo, keys, info, scope_info = agent2_dc_scoped(m, prof, args.max_parcels)
            else:  # no approved profile: previous behaviour (all records of the list layer)
                log("agent2", "warning", jurisdiction=jkey, note="no approved dataset profile: scope not applied")
                df, secs, geo = agent2_dc(m, args.max_parcels)
                keys = [g.get(kg) for g in geo]
        else:
            cats = approved_categories(jkey)
            df, secs, geo = agent2_fairfax(m, cats, args.max_parcels)
            keys = [g.get(kg) for g in geo]
            owner_ok = prof and any((a.get("owner") or {}).get("answer") == "yes" for a in prof.values())
            scope_info = {"reporting_entity": JURISDICTIONS[jkey]["reporting_entity"],
                          "scope_note": ("Owner not available in the open data (dataset profile approved by the "
                                         "auditor): all levels of government included, by land-use category.")
                          if prof and not owner_ok else "Public land by land-use category approved by the auditor."}
        df.to_csv(os.path.join(OUT, f"{jkey}_parcels.csv"), index=False)
        flags = [c for c in df.columns if c.startswith("flag_")]
        df[df[flags].fillna(False).astype(bool).any(axis=1)].to_csv(os.path.join(OUT, f"{jkey}_exceptions.csv"),
                                                                     index=False)
        if info is not None:
            info.to_csv(os.path.join(OUT, f"{jkey}_info_list.csv"), index=False)
        s = {**summarize(jkey, df, secs), **scope_info}
        summaries.append(s)
        log("agent2", "tests_completed", **s)
        quick_map(jkey, df, geo, keys)
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=1)
    print("\n" + json.dumps(summaries, indent=1))
    print(f"\nOutputs in ./{OUT}: *_parcels.csv, *_exceptions.csv, *_info_list.csv, summary.json, "
          "audit_trail.jsonl, *_map.png")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["agent1", "profile", "agent2"],
                    help="agent1 (field mapping), profile (Agent 1: what each dataset can prove) or agent2")
    ap.add_argument("--jurisdictions", nargs="+", default=["DC", "FAIRFAX"], choices=list(JURISDICTIONS))
    ap.add_argument("--llm", default="model", choices=["model", "reference"],
                    help="model = call the LLM in get_llm(); reference = offline reference (no LLM)")
    ap.add_argument("--model", default=None, help="LLM model as provider:model (default: env LLM_MODEL or openai:gpt-4.1)")
    ap.add_argument("--reuse-profile", action="store_true",
                    help="profile step: reuse outputs/profile/<J>_profile.json instead of reading the servers again")
    ap.add_argument("--max-parcels", type=int, default=1000, help="limit per jurisdiction (0 = no limit)")
    args = ap.parse_args()
    if not args.step:  # e.g. when run with the "Run" button of an IDE (Spyder, VS Code, Jupyter)
        choice = input("Which step? [1] agent1  [2] agent2  [3] agent1 offline (reference, no LLM)  "
                       "[4] profile: ").strip()
        args.step = {"2": "agent2", "4": "profile"}.get(choice, "agent1")
        if choice == "3":
            args.llm = "reference"
    global LLM_MODEL
    if args.model:
        LLM_MODEL = args.model
    if args.max_parcels == 0:
        args.max_parcels = None
    if args.step in ("agent1", "profile"):
        log("pipeline", "start", stage=args.step, jurisdictions=args.jurisdictions, llm=args.llm,
            model=LLM_MODEL if args.llm != "reference" else None)
    else:  # Agent 2 is deterministic: no LLM is called
        log("pipeline", "start", stage=args.step, jurisdictions=args.jurisdictions, llm="none (deterministic)")
    {"agent1": agent1, "profile": agent1_profile, "agent2": agent2}[args.step](args)


if __name__ == "__main__":
    main()
