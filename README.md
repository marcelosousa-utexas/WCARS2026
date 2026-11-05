# Agentic pilot: Fairfax County and Washington, DC

Research prototype of **Agents 1 and 2** from the WCARS 75 presentation, running on US open data.

| Agent | What it does | Uses an LLM? |
|---|---|---|
| **Agent 1, schema translation** | Reads only the **metadata** (field names, aliases and types) of the Fairfax and DC datasets and proposes how each field fits a single audit schema. For Fairfax, which does not publish owner names, it also proposes which land-use categories indicate public land. | Yes (any LLM) |
| **Human in the loop** | The auditor reviews the proposals in a spreadsheet and sets `approved = Y` or `N`. Agent 2 uses only what was approved. | No |
| **Agent 2, deterministic engine** | Downloads the public parcels, links the tax roll to the parcel shapes by key (SSL in DC; PARID to PIN in Fairfax), computes land area from the shape and lists the exceptions. | **No** |

Every step is written to `outputs/audit_trail.jsonl` (the audit trail). LLM prompts and answers are stored in `outputs/llm/`.

**Privacy by design:** the LLM never receives property records, only field names and the list of land-use categories.

## Scope

The pilot tests **land only**. Buildings and other improvements are out of scope: the area compared is land area, and the value read is the land value (`PHASELAND` in DC, `APRLAND` in Fairfax).

## Choose the LLM

The pipeline is provider-neutral. Set the model as `provider:model` and the provider's API key as an environment variable:

```bash
export LLM_MODEL="openai:gpt-4.1"          # or anthropic:..., google_genai:..., etc.
export OPENAI_API_KEY="..."                 # never write keys in the code
```

Or pass `--model provider:model` on the command line. To use another connector (for example an institutional gateway), create a private file `llm_local.py` with a function `get_llm(model)` that returns a LangChain chat model. It is listed in `.gitignore` and is never shared.

## Install

```bash
pip install requests pandas matplotlib langchain
pip install langchain-openai          # or the package of the provider you prefer
```

## Run

```bash
# 1) Agent 1, field mapping: proposes what each field means (calls the LLM)
python agentic_pilot.py agent1

# 2) Human review: open these files and fill the "approved" column with Y or N
#    outputs/review/DC_mapping_review.csv
#    outputs/review/FAIRFAX_mapping_review.csv
#    outputs/review/FAIRFAX_public_land_use_review.csv

# 3) Agent 1, dataset profile: what can each dataset prove? (calls the LLM once per jurisdiction)
python agentic_pilot.py profile

# 4) Human review: approve (Y / N) or correct each answer
#    outputs/review/DC_dataset_profile_review.csv
#    outputs/review/FAIRFAX_dataset_profile_review.csv

# 5) Agent 2: deterministic tests (start small)
python agentic_pilot.py agent2 --max-parcels 300
python agentic_pilot.py agent2 --max-parcels 0     # no limit
```

## Dataset profile: what can each dataset prove?

```bash
python agentic_pilot.py profile
```

This step belongs to Agent 1 and does not change the mapping review files.

1. **Profile (code, no LLM).** For each dataset, the code reads the official description, the fields and the most frequent values of each text field, with record counts. Data minimization: a value is kept only if it appears in at least 20 records (`K_MIN`, the "minimum cell size" rule of statistical disclosure control). Governments own many parcels, so their names pass; values that identify one person do not.
2. **Diagnosis (LLM).** For each dataset, the agent answers two questions, with evidence and confidence, or says `cannot_determine`:
   - Q1: Are the records land parcels, buildings or facilities, or mixed?
   - Q2: Does the dataset show the owner? Which values are the reporting entity?
3. **Check and impact (code, no LLM).** The code checks that the fields and values exist in the data and counts how many records each answer keeps or moves to an information list.
4. **Review (human).** Open `outputs/review/<J>_dataset_profile_review.csv`. For each row, set `approved` to Y or N, or write a correction in `auditor_note`. The column `guard_status` shows `ok`, `check` (the code found something to look at) or `rejected`.

**How Agent 2 uses the approved profile.** In DC, Agent 2 keeps only the records of the reporting entity, moves building or facility records and records of other owners to `outputs/DC_info_list.csv` (with the reason), takes land shapes from the dataset approved as `land_parcels`, and compares their area with the tax roll. In Fairfax, where the owner is not available, it keeps the approved land-use categories and states this limitation in `summary.json` (`scope_note`). If no profile review exists, Agent 2 runs as before. A new `profile` run never overwrites a review that already has approvals: the new proposal gets a `_proposed_<date>` suffix.

The reporting entity is part of the audit scope, set by the auditor in `JURISDICTIONS` (`reporting_entity`). Other outputs: `outputs/profile/<J>_profile.json` (the profile) and `outputs/llm/<J>_profile_prompt.txt` / `_response.txt`.

## Offline test

To test without the LLM, use `python agentic_pilot.py agent1 --llm reference`. It loads the reference mapping prepared with Claude (`reference_mapping_claude.json`). Comparing the output of your LLM with this reference is itself a useful control: do two models agree?

To change the model, use `--model provider:model`.

## Outputs (`outputs/` folder)

- `summary.json`: numbers for the slide (parcels tested, exceptions by type, run time)
- `DC_parcels.csv`, `FAIRFAX_parcels.csv`: result per parcel
- `DC_exceptions.csv`, `FAIRFAX_exceptions.csv`: only parcels with an exception
- `DC_map.png`, `FAIRFAX_map.png`: map of the parcels, divergent ones in red (needs matplotlib)
- `audit_trail.jsonl`: audit trail
- `llm/`: LLM prompts and answers

## Agent 2 tests

| Flag | Meaning |
|---|---|
| `flag_missing_key` | The shape has no parcel identifier (DC) |
| `flag_placeholder_key` | The identifier is not a real parcel key, e.g. "N/A" or "FREEWAY ROW" (DC) |
| `flag_no_geometry` | The record has no shape (DC) |
| `flag_not_in_tax_roll` | The parcel has no record in the tax roll (DC) |
| `flag_no_land_shape` | The parcel of the reporting entity has no shape in the land parcel dataset (DC) |
| `flag_no_polygon` | The public parcel in the tax roll has no shape (Fairfax) |
| `flag_one_to_many` / `flag_multi_polygon` | The link is not one-to-one |
| `flag_area_divergence` | Recorded land area differs from the area computed from the shape by more than 10% and more than 1,000 sq ft |
| `flag_missing_recorded_area` | No recorded land area (Fairfax) |
| `info_reservation_no_tax_lot` | Federal reservation without a tax lot, expected by design (DC) |
| `info_multi_polygon_key` | One parcel drawn as several shapes; areas are summed before the test (DC) |
| `info_method_check` | Our computed area differs by more than 2% from DC's own computed area: a check of our method (DC) |

Areas are computed in square feet, with data reprojected by the server: EPSG 2248 in DC and EPSG 2283 in Fairfax.

## Limitations (say them in the presentation)

- This is a research prototype, not a production system.
- In Fairfax, "public land" is defined by the land-use categories approved by the auditor, because owner names are not published. These categories include all levels of government.
- In DC, the "District Land" layer mixes land parcels with records of single buildings or facilities (for example, a charter school inside a larger federal parcel). These records can produce false area alerts.
- An exception is an **alert for the auditor to review**, not a conclusion. Some area differences have a legitimate explanation.
- API addresses may change. If one fails, the error message shows which one.

## If something fails

Send the error message and the file `outputs/audit_trail.jsonl`.

## Manual check of one parcel

`check_parcel_area.py` compares, for one Fairfax parcel, the recorded land area in the tax table with the area computed from the official parcel shape.

```bash
python check_parcel_area.py                   # default: 0191 01  0003 (Colvin Run)
python check_parcel_area.py "0502 10F 0001"
```
