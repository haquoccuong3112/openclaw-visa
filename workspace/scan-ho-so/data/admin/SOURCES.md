# `data/admin/` — Vietnamese administrative units (2025 reform) + old↔new mapping

Used by `lib/diadia.py` (`resolve_address` / `same_place` / `commune_merge_info`) and, indirectly,
by `lib/checklist.py` (the `_dia_gioi` annotation on the thẩm-định profile) and `lib/chat.py`
(`NEED_ADDR`). **No DB, no service** — these are flat JSON files loaded into memory on first use.

## Files
| File | What | Source | License |
|---|---|---|---|
| `province_new.json` | The 34 post-reform provincial units (6 TP trực thuộc TW + 28 tỉnh), dict keyed by code | [`vietmap-company/vietnam_administrative_address`](https://github.com/vietmap-company/vietnam_administrative_address) `admin_new/province.json`, **verbatim** | VietMap Administrative Data License |
| `ward_new.json` | The ~3,321 post-reform xã/phường, dict keyed by code (`parent_code` = province code) | same repo, `admin_new/ward.json`, **verbatim** | VietMap Administrative Data License |
| `admin_mapping_old_to_new.xlsx` | The official old-ward → new-ward+new-province crosswalk (10,358 rows), as published | same repo, `admin_mapping/admin_mapping_old_to_new_10_25.xlsx` (Oct 2025), **verbatim** | VietMap Administrative Data License |
| `old_to_new_wards.json` | The XLSX above, **mechanically JSON-ified** (`{columns:[...], rows:[[...],...]}`) — derived, not modified in content | derived from `admin_mapping_old_to_new.xlsx` via `_convert_xlsx.py` | derived from VietMap data — see license note below |
| `_convert_xlsx.py` | One-off script to (re)generate `old_to_new_wards.json` from the `.xlsx` (needs `openpyxl`, not a runtime dep) | ours | — |
| `../provinces_34.json` | Human-readable province-level summary (6 cities, 28 provinces, `old_to_new` province map, effective dates) — fed into the tầng-2 thẩm-định prompt by `lib/checklist.py:_provinces_text()` | generated from `province_new.json` + `old_to_new_wards.json` | — |

## License note (VietMap Administrative Data License)
The VietMap data may be used freely offline / commercially; **no attribution required for direct end-use**.
The only obligation: *if you **modify** the data and **redistribute** it*, the derivative must be released
under an open-source license and credit VietMap as the original source. We bundle the `.xlsx` and
`ward_new.json` / `province_new.json` **unmodified**; `old_to_new_wards.json` is a faithful JSON
transcription (no content change) of the `.xlsx`. This repo (`openclaw-visa`) is private (not a public
redistribution). If that ever changes, keep this note + credit VietMap.

## Updating
When Vietnam issues further administrative changes (more NQ UBTVQH), pull the latest files from the
VietMap repo, replace the four files above (keep the `.xlsx` filename `admin_mapping_old_to_new.xlsx`),
re-run `python3 _convert_xlsx.py` (in a venv with `openpyxl`), and regenerate `../provinces_34.json`.
