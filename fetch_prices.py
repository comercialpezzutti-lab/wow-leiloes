#!/usr/bin/env python3
"""Pull WoW Forever auction house prices from the Blizzard Game Data API.

Runs hourly from GitHub Actions. Uses only the standard library.

WoW Forever has no realms in the API: auctions hang off "rulesets"
(namespace dynamic-forever-{region}). This follows the community notes at
https://us.forums.blizzard.com/en/blizzard/t/wow-forever-api-what-works-so-far-plus-a-ts-client/60334
and is defensive about response shapes, since the API is undocumented.

Env:
  BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET  (required unless --demo)
  WOW_REGION      default from config.json ("us")
  RULESET_ID      optional, picks a ruleset; default is the first one
  HOUSE_IDS       optional, comma list of auction house ids to keep (else config "house_ids")
  HOUSE_FILTER    optional, words the house name may contain, "|" separated
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class Api:
    def __init__(self, region: str, client_id: str, secret: str) -> None:
        self.region = region
        self.base = f"https://{region}.api.blizzard.com"
        self.token = self._token(client_id, secret)

    def _token(self, client_id: str, secret: str) -> str:
        basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        req = urllib.request.Request(
            "https://oauth.battle.net/token",
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.load(res)["access_token"]

    def get(self, path: str, kind: str = "dynamic", params: dict | None = None) -> dict:
        namespace = f"{kind}-forever-{self.region}"
        query = {"locale": CONFIG.get("locale", "en_US"), **(params or {})}
        url = f"{self.base}{path}?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Battlenet-Namespace": namespace,
                "Accept-Encoding": "identity",
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as res:
                    return json.load(res)
            except urllib.error.HTTPError as err:
                if err.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        raise RuntimeError("unreachable")


def find_id_list(obj: object) -> list[dict]:
    """Return the first list of dicts that carry an 'id' (shape-agnostic)."""
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and "id" in obj[0]:
        return obj
    if isinstance(obj, dict):
        for value in obj.values():
            found = find_id_list(value)
            if found:
                return found
    return []


def text(value: object) -> str:
    if isinstance(value, dict):  # localized names can come back as {"en_US": ...}
        return str(next(iter(value.values()), ""))
    return str(value or "")


def summarize(entries: list[tuple[int, int]]) -> dict:
    """entries = [(unit_price_copper, quantity)] -> min, weighted median, totals."""
    entries = sorted(entries)
    total = sum(q for _, q in entries)
    half, running, median = total / 2, 0, entries[-1][0]
    for price, qty in entries:
        running += qty
        if running >= half:
            median = price
            break
    return {"min": entries[0][0], "median": median, "qty": total, "listings": len(entries)}


TIME_LEFT = {"SHORT": "S", "MEDIUM": "M", "LONG": "L", "VERY_LONG": "V"}


def collect_house(api: Api, ruleset: int, house: int, tracked: set[str]) -> tuple[dict, dict]:
    """Return (summaries per item, {auction_id: [item, qty, time_left]}) for tracked items."""
    prices: dict[str, list[tuple[int, int]]] = {}
    seen: dict[str, list] = {}

    def add(auc: dict, unit: object, qty: object) -> None:
        key = str((auc.get("item") or {}).get("id"))
        if key not in tracked:
            return
        qty = int(qty or 1)
        if auc.get("id") is not None:
            seen[str(auc["id"])] = [key, qty, TIME_LEFT.get(str(auc.get("time_left")), "?")]
        if unit:
            prices.setdefault(key, []).append((int(unit), qty))

    body = api.get(f"/data/wow/ruleset/{ruleset}/auctions/{house}")
    for auc in body.get("auctions") or []:
        qty = int(auc.get("quantity") or 1)
        buyout = auc.get("buyout") or auc.get("unit_price")
        unit = (auc["unit_price"] if "unit_price" in auc else int(buyout) // qty) if buyout else None
        add(auc, unit, qty)

    commodities = body.get("commodities")
    rows = commodities if isinstance(commodities, list) else None
    if rows is None:
        try:
            rows = api.get(f"/data/wow/ruleset/{ruleset}/auctions/{house}/commodities").get("auctions") or []
        except urllib.error.HTTPError as err:
            log(f"  commodities {house}: HTTP {err.code}")
            rows = []
    for auc in rows:
        add(auc, auc.get("unit_price"), auc.get("quantity"))

    return {key: summarize(val) for key, val in prices.items()}, seen


def estimate_sold(prev: dict, now: dict) -> dict[str, int]:
    """Units that left the auction house since the last run before running out of time.

    An auction gone while it still had MEDIUM/LONG/VERY_LONG time left was bought
    (or cancelled by its seller; the API cannot tell these apart). A stack that
    shrank was partly bought.
    """
    sold: dict[str, int] = {}
    for aid, (item, qty, left) in prev.items():
        cur = now.get(aid)
        if cur is None:
            if left in ("M", "L", "V"):
                sold[item] = sold.get(item, 0) + qty
        elif cur[1] < qty:
            sold[item] = sold.get(item, 0) + qty - cur[1]
    return sold


def demo_houses(tracked: set[str]) -> tuple[list[dict], dict]:
    base = {
        "2589": 60, "2592": 140, "4306": 300, "4338": 900, "14047": 2500,
        "2996": 150, "2997": 450, "4305": 1300, "4339": 4800, "14048": 4400,
        "10940": 900, "11083": 2600, "11137": 6000, "11176": 9000, "16204": 15000,
        "10938": 1100, "10939": 3600, "10998": 2200, "11082": 7000, "11134": 4000,
        "11135": 12000, "10978": 4000, "11084": 9000, "6338": 3500, "11128": 9000,
        "11144": 30000, "4238": 900, "5762": 3200, "4240": 2800, "4245": 7000,
        "10050": 30000, "14046": 90000, "20744": 900, "20746": 5500, "20745": 6000,
    }
    rng = random.Random(dt.datetime.now(dt.UTC).hour)
    items = {}
    for key in tracked:
        price = base.get(key, 1000)
        items[key] = {
            "min": int(price * rng.uniform(0.75, 0.95)),
            "median": int(price * rng.uniform(0.95, 1.15)),
            "qty": rng.randint(5, 400),
            "listings": rng.randint(1, 40),
        }
    return [{"id": 0, "name": "Casa de Leilões de exemplo (Aliança)", "items": items}], {"id": 0, "name": "demo"}


def main() -> int:
    demo = "--demo" in sys.argv
    region = os.environ.get("WOW_REGION") or CONFIG.get("region", "us")
    tracked = set(CONFIG["items"].keys())
    recipes_file = DATA / "recipes.json"
    if recipes_file.exists():
        tracked |= set(json.loads(recipes_file.read_text(encoding="utf-8"))["items"].keys())
    log(f"Itens acompanhados: {len(tracked)}")
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)

    if demo:
        houses, ruleset = demo_houses(tracked)
        all_houses = [{"id": 0, "name": houses[0]["name"]}]
    else:
        cid, secret = os.environ.get("BLIZZARD_CLIENT_ID"), os.environ.get("BLIZZARD_CLIENT_SECRET")
        if not cid or not secret:
            log("Faltam BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET (Segredos do repositório).")
            return 2
        api = Api(region, cid, secret)

        rulesets = find_id_list(api.get("/data/wow/ruleset/index"))
        if not rulesets:
            log("Nenhum ruleset retornado pela API.")
            return 1
        wanted = os.environ.get("RULESET_ID")
        ruleset_list = []
        for r in rulesets:
            name = text(r.get("name"))
            if not name:
                try:
                    name = text(api.get(f"/data/wow/ruleset/{r['id']}").get("name"))
                except urllib.error.HTTPError:
                    pass
            ruleset_list.append({"id": r["id"], "name": name})
            log(f"  ruleset {r['id']}: {name}")
        chosen = next((r for r in ruleset_list if str(r["id"]) == wanted), ruleset_list[0])
        ruleset = {**chosen, "all": ruleset_list}
        log(f"Usando ruleset {ruleset['id']} {ruleset['name']}")

        all_houses = [
            {"id": h["id"], "name": text(h.get("name"))}
            for h in find_id_list(api.get(f"/data/wow/ruleset/{ruleset['id']}/auctions/"))
        ]
        ids = {s.strip() for s in os.environ.get("HOUSE_IDS", "").split(",") if s.strip()} or {str(i) for i in CONFIG.get("house_ids") or []}
        flt = (os.environ.get("HOUSE_FILTER") or CONFIG.get("house_filter") or "").lower()
        terms = [t.strip() for t in flt.split("|") if t.strip()]
        if ids:
            selected = [h for h in all_houses if str(h["id"]) in ids]
        else:
            selected = [h for h in all_houses if any(t in h["name"].lower() for t in terms)]
        if not selected:
            log(f"Nenhuma casa com '{flt}' no nome; usando todas.")
            selected = all_houses
        log(f"Casas: {len(all_houses)} no total, {len(selected)} selecionadas")

        ids_file = DATA / "auction_ids.json"
        prev_ids = {}
        if ids_file.exists():
            saved = json.loads(ids_file.read_text(encoding="utf-8"))
            age = now - dt.datetime.fromisoformat(saved["time"].replace("Z", "+00:00"))
            if age <= dt.timedelta(hours=3):  # a longer gap would hide expiries
                prev_ids = saved["houses"]
        new_ids = {}
        houses = []
        for house in selected:
            try:
                items, seen = collect_house(api, ruleset["id"], house["id"], tracked)
                new_ids[str(house["id"])] = seen
                old = prev_ids.get(str(house["id"]))
                if old is not None:
                    for item, qty in estimate_sold(old, seen).items():
                        items.setdefault(item, {"min": None, "median": None, "qty": 0, "listings": 0})["sold"] = qty
                houses.append({**house, "items": items, "sold_known": old is not None})
                log(f"  casa {house['id']} {house['name']}: {len(items)} itens com preço")
            except urllib.error.HTTPError as err:
                log(f"  casa {house['id']}: HTTP {err.code}")
        if not houses:
            log("Nenhuma casa de leilões respondeu.")
            return 1

    DATA.mkdir(exist_ok=True)
    stamp = now.isoformat().replace("+00:00", "Z")
    if not demo:
        (DATA / "auction_ids.json").write_text(json.dumps({"time": stamp, "houses": new_ids}, separators=(",", ":")), encoding="utf-8")
    latest = {
        "generated_at": stamp,
        "demo": demo,
        "region": region,
        "ruleset": ruleset,
        "all_houses": all_houses,
        "houses": houses,
        "config": CONFIG,
    }
    (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")

    if not demo:
        # Compact snapshot per run: {house: {item: [min, median, qty]}}; keep 3 days.
        hist_dir = DATA / "history"
        hist_dir.mkdir(exist_ok=True)
        snap = {str(h["id"]): {i: [v["min"], v["median"], v["qty"], v.get("sold", 0)] for i, v in h["items"].items()} for h in houses}
        hour_file = hist_dir / f"{now:%Y-%m-%dT%H}.json"
        # More than one run per hour: keep the latest prices but add up the sold estimates.
        if hour_file.exists():
            try:
                earlier = json.loads(hour_file.read_text(encoding="utf-8"))
            except ValueError:
                earlier = {}
            for house_id, items in earlier.items():
                cur = snap.setdefault(house_id, {})
                for i, row in items.items():
                    sold = row[3] if len(row) > 3 else 0
                    if sold:
                        cur.setdefault(i, [None, None, 0, 0])[3] += sold
        hour_file.write_text(json.dumps(snap, separators=(",", ":")), encoding="utf-8")
        cutoff = f"{now - dt.timedelta(days=3):%Y-%m-%dT%H}"
        for old in hist_dir.iterdir():
            if old.stem < cutoff or old.suffix == ".csv":
                old.unlink()
        index = sorted(f.stem for f in hist_dir.glob("*.json"))
        (hist_dir / "index.json").unlink(missing_ok=True)
        index = [x for x in index if x != "index"]
        (DATA / "history_index.json").write_text(json.dumps(index), encoding="utf-8")
    log(f"OK: {stamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
