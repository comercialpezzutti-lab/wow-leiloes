#!/usr/bin/env python3
"""Download every WoW Forever profession recipe (reagents, output) from the
Blizzard Game Data API and save it to data/recipes.json.

Recipes change rarely, so this runs on demand (workflow input "recipes") and
the hourly price job reuses the saved file. Uses the static-forever-{region}
namespace; responses are read defensively because the API is undocumented.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_prices import CONFIG, DATA, Api, find_id_list, log, text  # noqa: E402

# Item subclass names (as the API returns them, en/pt) -> where players get it.
ORIGIN_HINTS = [
    (("herb", "erva", "herbal", "planta"), "Herborismo (colheita de ervas)"),
    (("poções", "potion", "elixir", "frasco", "flask"), "Alquimia"),
    (("comida", "food"), "Culinária"),
    (("explosiv",), "Engenharia"),
    (("missão", "quest"), "Item de missão ou de masmorra"),
    (("cloth", "tecido"), "Cai de inimigos humanoides"),
    (("metal", "stone", "minério", "pedra"), "Mineração"),
    (("leather", "couro"), "Esfolamento"),
    (("enchanting", "encantamento"), "Desencantar itens verdes ou melhores"),
    (("meat", "carne", "cooking", "culinária"), "Cai de feras / Pesca"),
    (("elemental",), "Cai de elementais"),
    (("parts", "peças", "devices", "explosives"), "Engenharia"),
]


def origin_hint(item: dict) -> str:
    sub = (text((item.get("item_subclass") or {}).get("name")) + " " + text((item.get("item_class") or {}).get("name"))).lower()
    for keys, hint in ORIGIN_HINTS:
        if any(k in sub for k in keys):
            return hint
    return ""


def main() -> int:
    region = os.environ.get("WOW_REGION") or CONFIG.get("region", "us")
    cid, secret = os.environ.get("BLIZZARD_CLIENT_ID"), os.environ.get("BLIZZARD_CLIENT_SECRET")
    if not cid or not secret:
        log("Faltam BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET.")
        return 2
    api = Api(region, cid, secret)

    def get(path: str) -> dict | None:
        try:
            return api.get(path, "static")
        except urllib.error.HTTPError as err:
            log(f"  {path}: HTTP {err.code}")
            return None

    index = get("/data/wow/profession/index") or {}
    professions = find_id_list(index)
    log(f"Profissões: {len(professions)}")

    recipe_refs: list[tuple[dict, str, str, int]] = []  # (ref, profession, tier, prof_id)
    prof_out = []
    for prof in professions:
        detail = get(f"/data/wow/profession/{prof['id']}") or {}
        pname = text(detail.get("name") or prof.get("name"))
        tiers = detail.get("skill_tiers") or []
        prof_out.append({"id": prof["id"], "name": pname, "type": text((detail.get("type") or {}).get("name"))})
        for tier in tiers:
            tier_detail = get(f"/data/wow/profession/{prof['id']}/skill-tier/{tier['id']}") or {}
            tname = text(tier_detail.get("name") or tier.get("name"))
            for cat in tier_detail.get("categories") or []:
                for rec in cat.get("recipes") or []:
                    recipe_refs.append((rec, pname, tname, prof["id"]))
        log(f"  {pname}: {sum(1 for r in recipe_refs if r[3] == prof['id'])} receitas")

    def load_recipe(entry: tuple[dict, str, str, int]) -> dict | None:
        ref, pname, tname, pid = entry
        body = get(f"/data/wow/recipe/{ref['id']}")
        if not body:
            return None
        crafted = body.get("crafted_item") or body.get("alliance_crafted_item") or {}
        qty = body.get("crafted_quantity") or {}
        reagents = {}
        names = {}
        for r in body.get("reagents") or []:
            rid = str((r.get("reagent") or {}).get("id"))
            reagents[rid] = int(r.get("quantity") or 1)
            names[rid] = text((r.get("reagent") or {}).get("name"))
        if crafted.get("id"):
            names[str(crafted["id"])] = text(crafted.get("name"))
        skill = None
        for key, val in body.items():  # any skill/rank requirement the API exposes
            if ("skill" in key or "rank" in key) and isinstance(val, (int, float)):
                skill = int(val)
        return {
            "id": ref["id"],
            "skill": skill,
            "_keys": sorted(body.keys()),
            "name": text(body.get("name") or ref.get("name")),
            "profession": pname,
            "profession_id": pid,
            "tier": tname,
            "makes": str(crafted["id"]) if crafted.get("id") else None,
            "qty": qty.get("value") or qty.get("minimum") or 1,
            "reagents": reagents,
            "_names": names,
        }

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        recipes = [r for r in pool.map(load_recipe, recipe_refs) if r]
    log(f"Receitas carregadas: {len(recipes)} de {len(recipe_refs)}")
    if recipes:
        log(f"Campos de uma receita: {recipes[0]['_keys']}")
    for rec in recipes:
        rec.pop("_keys", None)
    if not recipes:
        log("Nenhuma receita: a API do Forever pode não ter esses endpoints.")
        return 1

    items: dict[str, dict] = {}
    for rec in recipes:
        for iid, name in rec.pop("_names").items():
            items.setdefault(iid, {"name": name})
        for iid in rec["reagents"]:
            items[iid]["reagent"] = True
        if rec["makes"]:
            items[rec["makes"]]["crafted"] = True

    def load_item(iid: str) -> tuple[str, dict | None]:
        return iid, get(f"/data/wow/item/{iid}")

    reagent_ids = [i for i, v in items.items() if v.get("reagent") or v.get("crafted")]
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for iid, body in pool.map(load_item, reagent_ids):
            if body:
                items[iid]["origin"] = origin_hint(body)
                items[iid]["cls"] = " / ".join(
                    x for x in (text((body.get("item_class") or {}).get("name")), text((body.get("item_subclass") or {}).get("name"))) if x
                )
                items[iid]["vendor_buy"] = body.get("purchase_price")
                items[iid]["ilvl"] = body.get("level")
                items[iid]["req"] = body.get("required_level")
                if not items[iid]["name"]:
                    items[iid]["name"] = text(body.get("name"))

    # Where a recipe is learned. The API has no "source" field, so list every
    # recipe item (item class 9: "Molde: X", "Fórmula: X"...) and match names;
    # a recipe with no such item comes from the trainer.
    locale = CONFIG.get("locale", "pt_BR")
    by_name: dict[str, dict] = {}
    page = 1
    while True:
        try:
            res = api.get("/data/wow/search/item", "static", {"item_class.id": 9, "orderby": "id", "_pageSize": 1000, "_page": page})
        except urllib.error.HTTPError as err:
            log(f"  busca de moldes, página {page}: HTTP {err.code}")
            break
        for hit in res.get("results") or []:
            data = hit.get("data") or {}
            raw = data.get("name")
            hname = (raw.get(locale) or text(raw)) if isinstance(raw, dict) else text(raw)
            if ":" in hname:
                by_name.setdefault(hname.split(":", 1)[1].strip().lower(), {"id": data.get("id"), "name": hname})
        if page >= int(res.get("pageCount") or 1):
            break
        page += 1
    log(f"Itens de receita (moldes/fórmulas) encontrados: {len(by_name)}")

    def pattern_detail(entry: tuple[dict, dict]) -> tuple[dict, dict, dict]:
        rec, pat = entry
        return rec, pat, get(f"/data/wow/item/{pat['id']}") or {}

    matched = [(rec, by_name[rec["name"].lower()]) for rec in recipes if rec["name"] and rec["name"].lower() in by_name]
    for rec in recipes:
        rec["source"] = {"type": "trainer"}
    with cf.ThreadPoolExecutor(max_workers=6) as pool:
        for rec, pat, body in pool.map(pattern_detail, matched):
            preview = body.get("preview_item") or {}
            skill = ((preview.get("requirements") or body.get("requirements") or {}).get("skill") or {}).get("level")
            if skill and not rec.get("skill"):
                rec["skill"] = skill
            rec["source"] = {
                "type": "pattern",
                "item": pat["id"],
                "name": pat["name"],
                "buy": body.get("purchase_price"),
                "quality": text((body.get("quality") or {}).get("name") or (body.get("quality") or {}).get("type")),
                "bind": text((preview.get("binding") or {}).get("type")),
            }
            items.setdefault(str(pat["id"]), {"name": pat["name"]})["pattern"] = True
    log(f"Receitas com molde/fórmula: {len(matched)}; do instrutor: {len(recipes) - len(matched)}")

    DATA.mkdir(exist_ok=True)
    out = {"professions": prof_out, "recipes": recipes, "items": items}
    (DATA / "recipes.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"OK: {len(recipes)} receitas, {len(items)} itens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
