import html as htmllib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
OUT_FILE = ROOT / "docs" / "analogs.json"

STORE_API = "https://store.tildaapi.com/api/getproductslist/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DELAY = float(CONFIG.get("request_delay_sec", 3))

RAW_DATA_RE = re.compile(r'const\s+rawData\s*=\s*"((?:[^"\\]|\\.)*)"', re.S)
SECTION_RE = re.compile(r'<section[^>]*class="hp-analogs"[^>]*>(.*?)</section>', re.S)
TITLE_RE = re.compile(r'<h3[^>]*hp-analogs__title[^>]*>(.*?)</h3>', re.S)
ROW_RE = re.compile(
    r'<div class="hp-analogs__row(?![^"]*--header)[^"]*">\s*'
    r'<span[^>]*hp-analogs__cell--brand[^>]*>(.*?)</span>\s*'
    r'<span[^>]*hp-analogs__cell--art[^>]*>(.*?)</span>', re.S)
TAG_RE = re.compile(r"<[^>]+>")


class Blocked(Exception):
    pass


def http_get(url, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept-Language": "ru,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                raise
            wait = 60 * (attempt + 1) if e.code in (403, 410, 429) else 5 * (attempt + 1)
        except Exception as e:
            last = e
            wait = 5 * (attempt + 1)
        if attempt < retries - 1:
            print(f"    ! {url}: {last}, жду {wait} c…")
            time.sleep(wait)
    raise Blocked(f"{url}: {last}")


def site_base():
    return CONFIG["site_url"].rstrip("/")


def find_storeparts():
    if CONFIG.get("storeparts"):
        return [str(x) for x in CONFIG["storeparts"]]
    found = []
    for path in CONFIG.get("catalog_pages", ["/catalog"]):
        page = http_get(site_base() + path)
        for sp in re.findall(r"storepart:'(\d+)'", page):
            if sp not in found:
                found.append(sp)
        time.sleep(DELAY)
    return found


def fetch_products():
    products = {}
    for part in find_storeparts():
        slice_ = 1
        while slice_:
            q = urllib.parse.urlencode({"storepartuid": part, "slice": slice_, "size": 100})
            data = json.loads(http_get(f"{STORE_API}?{q}"))
            for p in data.get("products", []):
                products[str(p["uid"])] = p
            slice_ = data.get("nextslice") or 0
            time.sleep(0.5)
    return list(products.values())


def product_path(p):
    url = p.get("url") or p.get("buttonlink") or ""
    return urllib.parse.urlparse(url).path if url else ""


def first_image(p):
    try:
        gallery = json.loads(p.get("gallery") or "[]")
        return gallery[0]["img"] if gallery else ""
    except (ValueError, KeyError, IndexError, TypeError):
        return ""


def clean(s):
    return " ".join(htmllib.unescape(TAG_RE.sub(" ", s)).split())


def has_digit(tok):
    return any(c.isdigit() for c in tok)


def parse_raw_string(raw, known_brands):
    tokens = raw.split()
    pairs, brand_words = [], []
    for tok in tokens:
        if not has_digit(tok):
            if pairs and not brand_words and len(tok) <= 2 and tok.isalpha() and tok.islower():
                b, a = pairs[-1]
                pairs[-1] = (b, f"{a} {tok}")
            else:
                brand_words.append(tok)
            continue
        art_prefix = []
        brand = pairs[-1][0] if pairs and not brand_words else ""
        if brand_words:
            j = len(brand_words)
            while j > 1 and brand_words[j - 1].isupper() and len(brand_words[j - 1]) <= 4 \
                    and " ".join(brand_words[: j - 1]) in known_brands:
                j -= 1
            art_prefix = brand_words[j:]
            brand = " ".join(brand_words[:j])
            brand_words = []
        pairs.append((brand, " ".join(art_prefix + [tok])))
    return pairs


def split_cell(a):
    out = []
    for piece in re.split(r"[,;]", a):
        piece = piece.strip()
        if not piece:
            continue
        words = piece.split()
        if len(words) > 1 and all(has_digit(w) and len(w) >= 5 for w in words):
            out.extend(words)
        else:
            out.append(piece)
    return out


def extract_page(page):
    rows = []
    for section in SECTION_RE.findall(page):
        parts = re.split(r'(?=<h3[^>]*hp-analogs__title)', section)
        for part in parts:
            t = TITLE_RE.search(part)
            group = clean(t.group(1)) if t else ""
            for b, a in ROW_RE.findall(part):
                b, a = clean(b), clean(a)
                if a and a.lower() != "артикул":
                    rows.extend((group, b, x) for x in split_cell(a))
    raws = [json.loads(f'"{s}"') for s in RAW_DATA_RE.findall(page)]
    return rows, raws


def normalize(s):
    s = s.upper().translate(str.maketrans("АВЕКМНОРСТХУ", "ABEKMHOPCTXY"))
    return re.sub(r"[^0-9A-ZА-ЯЁ]", "", s)


def group_label(title):
    m = re.search(r"для\s+(\S+)", title, re.I)
    return m.group(1) if m else ""


def load_previous():
    if not OUT_FILE.exists():
        return None
    try:
        return json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except ValueError:
        return None


def previous_entries_by_sku(prev):
    out = {}
    if not prev:
        return out
    for e in prev.get("analogs", []):
        b, a, pi = e[0], e[1], e[2]
        g = e[3] if len(e) > 3 else ""
        sku = prev["products"][pi]["sku"]
        out.setdefault(sku, []).append((prev["brands"][b], a, g))
    return out


def main():
    print(f"Сайт: {site_base()}  (пауза между страницами {DELAY} c)")
    products = sorted(fetch_products(), key=lambda x: x.get("sku") or "")
    print(f"Товаров в каталоге: {len(products)}")
    if not products:
        sys.exit("Каталог пустой — что-то не так, файл не перезаписываю.")

    prev = load_previous()
    prev_by_sku = previous_entries_by_sku(prev)

    pages = {}
    problems = []
    for n, p in enumerate(products, 1):
        uid, sku, path = str(p["uid"]), p.get("sku") or "", product_path(p)
        label = f"[{n}/{len(products)}] {sku}"
        if not path:
            problems.append(f"{sku}: в каталоге не указана ссылка на страницу")
            pages[uid] = None
            continue
        try:
            rows, raws = extract_page(http_get(site_base() + path))
            pages[uid] = (rows, raws)
            cnt = len(rows) + sum(len(r.split()) // 2 for r in raws)
            print(f"  {label}: ~{cnt} аналогов" if cnt else f"  {label}: аналогов нет")
            if not cnt:
                problems.append(f"{sku} ({path}): на странице нет таблицы аналогов")
        except urllib.error.HTTPError as e:
            pages[uid] = None
            problems.append(f"{sku} ({path}): страница не открывается (HTTP {e.code}) — не опубликована?")
            print(f"  {label}: HTTP {e.code}")
        except Blocked as e:
            pages[uid] = None
            problems.append(f"{sku} ({path}): не скачалась ({e}) — взяты данные из прошлой базы")
            print(f"  {label}: не скачалась")
        time.sleep(DELAY)

    failed = sum(1 for v in pages.values() if v is None)
    if failed > len(products) * 0.5:
        sys.exit(f"Не скачалось {failed} страниц из {len(products)} — похоже, Тильда блокирует. "
                 f"Файл не перезаписываю, попробуйте позже или увеличьте request_delay_sec.")

    known = {b for v in pages.values() if v for _, b, _ in v[0] if b}
    for v in pages.values():
        if v:
            for raw in v[1]:
                known.update(b for b, _ in parse_raw_string(raw, set()) if b)

    brands, brand_idx = [], {}
    groups, group_idx = [""], {"": 0}
    out_products, entries, seen = [], [], set()

    def idx(name, lst, d):
        if name not in d:
            d[name] = len(lst)
            lst.append(name)
        return d[name]

    for p in products:
        uid, sku = str(p["uid"]), p.get("sku") or ""
        pi = len(out_products)
        out_products.append({
            "sku": sku,
            "title": p.get("title") or "",
            "brand": p.get("brand") or "",
            "url": product_path(p),
            "img": first_image(p),
        })
        page = pages.get(uid)
        if page is None:
            triples = prev_by_sku.get(sku, [])
        else:
            rows, raws = page
            triples = [(b, a, group_label(g)) for g, b, a in rows]
            for raw in raws:
                triples += [(b, a, "") for b, a in parse_raw_string(raw, known)]
        for b, a, g in triples:
            key = (normalize(a), b.upper(), pi)
            if not key[0] or key in seen:
                continue
            seen.add(key)
            e = [idx(b, brands, brand_idx), a, pi]
            gi = idx(g, groups, group_idx)
            if gi:
                e.append(groups[gi])
            entries.append(e)

    if prev:
        old_n = len(prev.get("analogs", []))
        if old_n and len(entries) < old_n * float(CONFIG.get("min_ratio_vs_previous", 0.7)):
            sys.exit(f"Аналогов стало {len(entries)} против {old_n} раньше — подозрительно мало, "
                     f"файл не перезаписываю. Если удаление было намеренным, удалите docs/analogs.json и запустите снова.")

    result = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site": site_base(),
        "brands": brands,
        "products": out_products,
        "analogs": entries,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    with_analogs = len({e[2] for e in entries})
    print()
    print(f"Товаров: {len(out_products)}, из них с аналогами: {with_analogs}")
    print(f"Записей аналогов: {len(entries)}, брендов: {len(brands)}")
    print(f"Файл: {OUT_FILE} ({OUT_FILE.stat().st_size // 1024} КБ)")
    report = ROOT / "docs" / "report.txt"
    report.write_text(
        f"Обновлено: {result['updated']}\nТоваров: {len(out_products)}, с аналогами: {with_analogs}\n"
        f"Записей аналогов: {len(entries)}\n\nПроблемы:\n" + ("\n".join(problems) or "нет") + "\n",
        encoding="utf-8")
    if problems:
        print(f"\nПроблемы ({len(problems)}), полный список в docs/report.txt:")
        for w in problems:
            print("  - " + w)


if __name__ == "__main__":
    main()
