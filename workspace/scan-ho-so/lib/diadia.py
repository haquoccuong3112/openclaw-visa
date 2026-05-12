"""Tra cứu địa giới hành chính Việt Nam cũ ↔ mới (cải cách 2025) — deterministic.

Đọc từ ../data/admin/ (province_new.json, ward_new.json, old_to_new_wards.json) + ../data/provinces_34.json.
KHÔNG phải HTTP service — lib/checklist.py và lib/chat.py import vào dùng. Bảng được load lazy, cache trong RAM.

API:
  resolve_address(text)            → {raw, tinh_moi, xa_moi, is_old_province, is_old_ward, ghi_chu, confidence, candidates}
  same_place(a, b)                 → (verdict ∈ {"same","different","unknown"}, giải_thích)
  commune_merge_info(name, prov=None) → {found, old_name, new_ward, new_province, ghi_chu, candidates}

Sự thật cứng (ai merge vào ai) đến từ bảng VietMap (xem data/admin/SOURCES.md). Resolver TRẢ THẬT THÀ
confidence ("exact"/"fuzzy"/"province_only"/"unknown") — không giả vờ bảng đầy đủ; ca mờ để LLM xử lý tiếp.
"""
from __future__ import annotations
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "data"   # <scan-ho-so>/data
_ADM = _DATA / "admin"

# ── normalisation ────────────────────────────────────────────────────────────
_PREFIX_RE = re.compile(
    r"^\s*(thành\s*phố|tp\.?|thành\s*phố\s*trực\s*thuộc|thị\s*xã|tx\.?|thị\s*trấn|tt\.?|"
    r"quận|q\.?|huyện|h\.?|phường|p\.?|xã|x\.?|tỉnh|t\.?)\s+",
    re.IGNORECASE,
)


def _fold(s) -> str:
    """lowercase, bỏ dấu (đ→d), bỏ dấu câu, gộp khoảng trắng — chuỗi để so khớp."""
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.replace("Đ", "D").replace("đ", "d").casefold()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_part(s) -> str:
    """Bỏ tiền tố hành chính (Phường/Xã/Quận/Huyện/TP/Tỉnh…) khỏi MỘT phần địa chỉ, rồi _fold."""
    s = str(s or "").strip()
    prev = None
    while s != prev:
        prev = s
        s = _PREFIX_RE.sub("", s, count=1).strip()
    return _fold(s)


def _strip_prefix(s) -> str:
    """Như normalize_part nhưng giữ nguyên chữ + dấu (chỉ bỏ tiền tố) — dùng cho output."""
    s = str(s or "").strip()
    prev = None
    while s != prev:
        prev = s
        s = _PREFIX_RE.sub("", s, count=1).strip()
    return s


# ── bảng + index (lazy, cache) ───────────────────────────────────────────────
@lru_cache(maxsize=1)
def _tables() -> dict:
    prov_new = json.loads((_ADM / "province_new.json").read_text("utf-8"))   # {code: {name,type,...}}
    ward_new = json.loads((_ADM / "ward_new.json").read_text("utf-8"))       # {code: {name,parent_code,...}}
    raw = json.loads((_ADM / "old_to_new_wards.json").read_text("utf-8"))    # {columns:[...], rows:[[...]]}
    summary = json.loads((_DATA / "provinces_34.json").read_text("utf-8"))   # {cities,provinces,old_to_new,...}
    col = {c: i for i, c in enumerate(raw["columns"])}
    rows = raw["rows"]

    # tên tỉnh MỚI (folded) -> tên chuẩn
    prov_by_fold: dict[str, str] = {}
    for p in prov_new.values():
        prov_by_fold[normalize_part(p["name"])] = p["name"]
    for nm in summary.get("cities", []) + summary.get("provinces", []):
        prov_by_fold.setdefault(normalize_part(nm), nm)

    # tỉnh CŨ (folded) -> tỉnh MỚI (tên chuẩn)
    old_prov_to_new: dict[str, str] = {}
    for o, n in (summary.get("old_to_new") or {}).items():
        old_prov_to_new[normalize_part(o)] = prov_by_fold.get(normalize_part(n), n)

    # xã/phường MỚI: (folded tên tỉnh, folded tên xã) -> [ {ward, province, ward_code, province_code} ]
    ward_new_idx: dict[tuple, list] = {}
    for w in ward_new.values():
        pname = prov_new.get(str(w.get("parent_code", "")), {}).get("name", "")
        ward_new_idx.setdefault((normalize_part(pname), normalize_part(w["name"])), []).append({
            "ward": w.get("name_with_type") or w["name"], "province": pname,
            "ward_code": w.get("code"), "province_code": w.get("parent_code"),
        })

    # xã CŨ -> [ {new_ward, new_province, new_ward_code} ]:
    #   idx3 key = (folded tỉnh cũ, folded huyện cũ, folded xã cũ);  idx2 key = (folded tỉnh cũ, folded xã cũ)
    old_ward_idx3: dict[tuple, list] = {}
    old_ward_idx2: dict[tuple, list] = {}
    for r in rows:
        op, od, ow = r[col["city_name_old"]], r[col["district_name_old"]], r[col["ward_name_old"]]
        np_, nw = r[col["city_name_new"]], r[col["ward_new_name"]]
        fp, fd, fw = normalize_part(op), normalize_part(od), normalize_part(ow)
        cn_prov = prov_by_fold.get(normalize_part(np_), _strip_prefix(np_))
        rec = {"new_ward": str(nw).strip(), "new_province": cn_prov, "new_ward_code": r[col["ward_id_new"]],
               "old_ward": str(ow).strip(), "old_district": str(od).strip(), "old_province": _strip_prefix(op)}
        if fp and fw:
            old_ward_idx3.setdefault((fp, fd, fw), []).append(rec)
            old_ward_idx2.setdefault((fp, fw), []).append(rec)
        old_prov_to_new.setdefault(fp, cn_prov)

    return {
        "prov_new": prov_new, "ward_new": ward_new, "summary": summary,
        "prov_by_fold": prov_by_fold, "old_prov_to_new": old_prov_to_new,
        "ward_new_idx": ward_new_idx, "old_ward_idx3": old_ward_idx3, "old_ward_idx2": old_ward_idx2,
    }


def _dedup(recs: list) -> list:
    seen, out = set(), []
    for r in recs:
        k = (r["new_ward"], r["new_province"])
        if k not in seen:
            seen.add(k); out.append(r)
    return out


# ── resolve một địa chỉ ──────────────────────────────────────────────────────
def _split_parts(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\s*[,;|]\s*|\s+-\s+", str(text or "")) if p.strip()]


def resolve_address(text: str) -> dict:
    """Phân tích một chuỗi địa chỉ (có thể lộn xộn / thiếu cấp / tên cũ) → đơn vị MỚI tương ứng.
    Heuristic: phần khớp tên TỈNH (mới hoặc cũ) là cấp tỉnh; phần ĐẦU thường là xã/phường."""
    raw = str(text or "").strip()
    base = {"raw": raw, "tinh_moi": "", "xa_moi": "", "is_old_province": False, "is_old_ward": False,
            "ghi_chu": "", "confidence": "unknown", "candidates": []}
    if not raw:
        return base
    T = _tables()
    parts = _split_parts(raw)
    if not parts:
        return base

    # --- cấp tỉnh: phần cuối cùng khớp tên tỉnh (ưu tiên mới, rồi cũ) ---
    prov_canon = ""; is_old_prov = False; prov_idx = None; prov_raw_part = ""
    for i in range(len(parts) - 1, -1, -1):
        fp = normalize_part(parts[i])
        if fp in T["prov_by_fold"]:
            prov_canon, prov_idx, prov_raw_part = T["prov_by_fold"][fp], i, parts[i]; break
        if fp in T["old_prov_to_new"]:
            prov_canon, is_old_prov, prov_idx, prov_raw_part = T["old_prov_to_new"][fp], True, i, parts[i]; break

    notes = []
    if is_old_prov:
        notes.append(f"«{_strip_prefix(prov_raw_part)}» là tên cấp tỉnh trước cải cách 2025 — nay thuộc «{prov_canon}» (hiệu lực 12/06/2025)")

    # --- cấp xã/phường: thử phần [0] (rồi [1] nếu [0] không ra) ---
    ward_canon = ""; is_old_ward = False; cands = []; ward_conf = None
    fp_for_lookup = normalize_part(prov_raw_part) if prov_idx is not None else None
    for wi in (0, 1):
        if wi >= len(parts) or (prov_idx is not None and wi >= prov_idx):
            break
        fw = normalize_part(parts[wi])
        fd = normalize_part(parts[wi + 1]) if (wi + 1 < len(parts) and (prov_idx is None or wi + 1 < prov_idx)) else ""
        # 1) đã là xã MỚI trong tỉnh này?
        if fp_for_lookup:
            hit = T["ward_new_idx"].get((normalize_part(prov_canon), fw)) or T["ward_new_idx"].get((fp_for_lookup, fw))
            if hit:
                ward_canon = hit[0]["ward"]; ward_conf = "exact"; break
        # 2) là xã CŨ? thử idx3 (có huyện) rồi idx2 (không huyện)
        keys3 = []
        if fp_for_lookup:
            keys3 += [(fp_for_lookup, fd, fw), (normalize_part(prov_canon), fd, fw)]
        keys2 = [(fp_for_lookup, fw), (normalize_part(prov_canon), fw)] if fp_for_lookup else []
        recs = []
        for k in keys3:
            recs += T["old_ward_idx3"].get(k, [])
        if not recs:
            for k in keys2:
                recs += T["old_ward_idx2"].get(k, [])
        recs = _dedup(recs)
        if recs:
            is_old_ward = True
            if len(recs) == 1:
                ward_canon = recs[0]["new_ward"]; ward_conf = "exact"
                if not prov_canon:
                    prov_canon = recs[0]["new_province"]
                notes.append(f"«{_strip_prefix(parts[wi])}» (cũ) nay là «{ward_canon}, {recs[0]['new_province']}» (sáp nhập xã, hiệu lực 01/07/2025)")
            else:
                cands = [{"new_ward": r["new_ward"], "new_province": r["new_province"]} for r in recs]
                ward_conf = "fuzzy"
                notes.append(f"«{_strip_prefix(parts[wi])}» (cũ) bị tách/nhập sang nhiều xã mới: " +
                             "; ".join(f"{r['new_ward']}, {r['new_province']}" for r in recs[:6]) +
                             (" …" if len(recs) > 6 else ""))
            break

    # --- confidence tổng hợp ---
    if prov_canon and ward_conf == "exact":
        conf = "exact"
    elif prov_canon and ward_conf == "fuzzy":
        conf = "fuzzy"
    elif prov_canon:
        conf = "province_only"
        if not ward_conf:
            notes.append("không xác định được cấp xã/phường từ chuỗi này (chỉ chắc cấp tỉnh)")
    else:
        conf = "unknown"
        notes.append("không nhận ra cấp tỉnh trong chuỗi địa chỉ")

    return {
        "raw": raw, "tinh_moi": prov_canon, "xa_moi": ward_canon,
        "is_old_province": is_old_prov, "is_old_ward": is_old_ward,
        "ghi_chu": " · ".join(notes), "confidence": conf, "candidates": cands,
    }


# ── so 2 địa chỉ ─────────────────────────────────────────────────────────────
def same_place(a: str, b: str) -> tuple[str, str]:
    """('same'|'different'|'unknown', giải thích). 'same' nếu cùng đơn vị mới ở mức xác định được
    (một bên là tên cũ của bên kia → vẫn 'same')."""
    ra, rb = resolve_address(a), resolve_address(b)
    if ra["confidence"] == "unknown" or rb["confidence"] == "unknown":
        return "unknown", f"không phân giải được: {ra['raw']!r}={ra['confidence']}, {rb['raw']!r}={rb['confidence']}"
    if normalize_part(ra["tinh_moi"]) != normalize_part(rb["tinh_moi"]):
        return "different", f"khác cấp tỉnh: «{ra['tinh_moi']}» vs «{rb['tinh_moi']}»"
    # cùng tỉnh; so cấp xã nếu cả hai có
    wa, wb = normalize_part(ra["xa_moi"]), normalize_part(rb["xa_moi"])
    if wa and wb:
        if wa == wb:
            note = "cùng đơn vị mới"
            if ra["is_old_province"] or ra["is_old_ward"] or rb["is_old_province"] or rb["is_old_ward"]:
                note += " (một bên dùng tên trước cải cách 2025)"
            return "same", f"{note}: «{ra['xa_moi']}, {ra['tinh_moi']}»"
        return "different", f"cùng tỉnh «{ra['tinh_moi']}» nhưng khác xã/phường: «{ra['xa_moi']}» vs «{rb['xa_moi']}»"
    # chỉ chắc cấp tỉnh ở (ít nhất) một bên
    note = f"cùng cấp tỉnh «{ra['tinh_moi']}»; chưa đối chiếu được cấp xã/phường"
    if ra["is_old_province"] or rb["is_old_province"]:
        note += " (một bên dùng tên tỉnh cũ — không phải mâu thuẫn về địa giới)"
    return "same", note   # cùng tỉnh, dưới đó chưa rõ → coi là khớp ở mức cấp tỉnh


# ── tra một xã/phường cũ → mới (cho chat) ────────────────────────────────────
def commune_merge_info(name: str, province: str | None = None) -> dict:
    """Tên một xã/phường (có thể kèm tỉnh) → nó nay thuộc xã/phường + tỉnh nào.
    Trả {found, query, old_name, new_ward, new_province, ghi_chu, candidates}."""
    T = _tables()
    out = {"found": False, "query": str(name or "").strip(), "old_name": "", "new_ward": "",
           "new_province": "", "ghi_chu": "", "candidates": []}
    raw = str(name or "").strip()
    if not raw:
        return out
    parts = _split_parts(raw)
    fw = normalize_part(parts[0]) if parts else ""
    # tỉnh: từ tham số, hoặc phần cuối của chuỗi — giữ CẢ tên-như-gõ lẫn tên-mới-tương-ứng để tra cả 2 chiều
    prov_keys: list[str] = []   # các folded tên tỉnh để thử (rỗng = không biết tỉnh)
    for cand in ([province] if province else []) + ([parts[-1]] if len(parts) > 1 else []):
        pf = normalize_part(cand)
        if not pf:
            continue
        if pf in T["prov_by_fold"] or pf in T["old_prov_to_new"]:
            if pf not in prov_keys:
                prov_keys.append(pf)
            if pf in T["old_prov_to_new"]:
                nf = normalize_part(T["old_prov_to_new"][pf])
                if nf and nf not in prov_keys:
                    prov_keys.append(nf)
    # 1) đã là xã MỚI?
    cur = []
    if prov_keys:
        for pk in prov_keys:
            cur += T["ward_new_idx"].get((pk, fw), [])
    else:
        for (pp, ww), v in T["ward_new_idx"].items():
            if ww == fw:
                cur += v
    cur = [{"new_ward": r["ward"], "new_province": r["province"]} for r in _dedup(
        [{"new_ward": x["ward"], "new_province": x["province"]} for x in cur])]
    # 2) xã CŨ?  (tra theo tỉnh CŨ trước; nếu không ra thì quét theo tên xã, khớp tỉnh CŨ hoặc tỉnh MỚI)
    recs = []
    if prov_keys:
        for pk in prov_keys:
            recs += T["old_ward_idx2"].get((pk, fw), [])
    if not recs:
        for (pp, ww), v in T["old_ward_idx2"].items():
            if ww != fw:
                continue
            if not prov_keys or pp in prov_keys or any(normalize_part(x["new_province"]) in prov_keys for x in v):
                recs += v
    recs = _dedup(recs)
    if recs:
        out["found"] = True
        out["old_name"] = recs[0]["old_ward"]
        if len(recs) == 1:
            out["new_ward"] = recs[0]["new_ward"]; out["new_province"] = recs[0]["new_province"]
            out["ghi_chu"] = (f"«{recs[0]['old_ward']}» ({recs[0]['old_district']}, {recs[0]['old_province']}) "
                              f"nay là «{recs[0]['new_ward']}, {recs[0]['new_province']}» (sáp nhập xã, hiệu lực 01/07/2025)")
        else:
            out["candidates"] = [{"new_ward": r["new_ward"], "new_province": r["new_province"]} for r in recs]
            out["ghi_chu"] = (f"«{recs[0]['old_ward']}» bị tách/nhập sang nhiều xã mới: " +
                              "; ".join(f"{r['new_ward']}, {r['new_province']}" for r in recs))
        return out
    if cur:
        out["found"] = True
        if len(cur) == 1:
            out["new_ward"], out["new_province"] = cur[0]["new_ward"], cur[0]["new_province"]
            out["old_name"] = cur[0]["new_ward"]
            out["ghi_chu"] = f"«{cur[0]['new_ward']}, {cur[0]['new_province']}» là đơn vị hiện hành (sau cải cách 2025) — không phải tên cũ"
        else:
            out["candidates"] = cur
            out["ghi_chu"] = "có nhiều xã/phường hiện hành trùng tên: " + "; ".join(f"{c['new_ward']}, {c['new_province']}" for c in cur)
        return out
    out["ghi_chu"] = f"không tìm thấy «{raw}» trong bảng địa giới (cũ lẫn mới) — có thể sai tên/tỉnh, hoặc cần kiểm tra văn bản chính thức"
    return out


# ── self-check ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    T = _tables()
    print("provinces (new):", len(T["prov_new"]), "| wards (new):", len(T["ward_new"]),
          "| old→new ward rows:", sum(len(v) for v in T["old_ward_idx2"].values()),
          "| old provinces mapped:", len(T["old_prov_to_new"]))
    # 1) tỉnh cũ → tỉnh mới
    r = resolve_address("Xã Hợp Thịnh, Huyện Tam Dương, Tỉnh Vĩnh Phúc")
    print("Vĩnh Phúc →", r["tinh_moi"], "| is_old_prov:", r["is_old_province"], "| conf:", r["confidence"], "|", r["ghi_chu"][:120])
    assert r["tinh_moi"] == "Phú Thọ" and r["is_old_province"] is True
    # 2) tỉnh không đổi
    r2 = resolve_address("Phường Hà Huy Tập, Thành phố Vinh, Nghệ An")
    print("Nghệ An →", r2["tinh_moi"], "| is_old_prov:", r2["is_old_province"], "| conf:", r2["confidence"])
    assert r2["tinh_moi"] == "Nghệ An" and r2["is_old_province"] is False
    # 3) same_place: cũ vs mới cùng nơi
    v, why = same_place("..., Tỉnh Vĩnh Phúc", "..., Phú Thọ")
    print("same_place(VP_cũ, PhúThọ):", v, "|", why)
    assert v == "same"
    # 4) khác tỉnh
    v2, _ = same_place("..., Nghệ An", "..., Hà Nội")
    print("same_place(NghệAn, HàNội):", v2)
    assert v2 == "different"
    # 5) rác
    v3, _ = same_place("zzz qqq", "abc")
    assert v3 == "unknown"
    # 6) commune_merge_info — lấy một xã cũ thật từ bảng để test
    sample_old = next(iter(T["old_ward_idx2"].items()))   # ((fp,fw), [recs])
    (fp_s, fw_s), recs_s = sample_old
    info = commune_merge_info(recs_s[0]["old_ward"], recs_s[0]["old_province"])
    print("commune_merge_info sample:", info["found"], "|", info["ghi_chu"][:140])
    assert info["found"] is True
    # 7) resolve_address rỗng
    assert resolve_address("")["confidence"] == "unknown"
    print("OK")
