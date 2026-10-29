"""
Dunyo rahbarlari kuzatuvchi bot — TEKIN versiya (GitHub Actions).

GitHub har 5 daqiqada shu faylni ishga tushiradi:
  1) Telegram kanallarning ochiq sahifalarini (t.me/s/...) o'qiydi,
  2) Google News'dan rahbarlar haqidagi yangiliklarni oladi,
  3) yangilarini Telegram bot orqali sizga yuboradi.
Hech qanday akkaunt, server yoki pul kerak emas.
"""

import html
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import requests
from bs4 import BeautifulSoup

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_IDS = [c.strip() for c in os.environ["CHAT_IDS"].split(",") if c.strip()]

BASE = Path(__file__).parent
SEEN_FILE = BASE / "seen.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
MAX_POSTS_PER_RUN = 40  # bir martada ko'pi bilan shuncha post yuboriladi

# Gemini (tekin) — o'zbekcha qisqa mazmun uchun. Kalit bo'lmasa, oddiy tarjima ishlatiladi.
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [m for m in [os.getenv("GEMINI_MODEL", "").strip(),
                             "gemini-flash-lite-latest", "gemini-flash-latest"] if m]

GOOGLE_QUERIES = [
    '"state visit" president',
    '"official visit" president OR "prime minister"',
    'president "held talks" OR "met with"',
    'summit "heads of state" OR leaders',
    'president "signed" agreement OR deal',
    'king OR emir OR chancellor "visit"',
    'официальный визит президент',
    'президент переговоры подписал',
]


# ---------------------------------------------------------------- yordamchilar

def read_list(name: str) -> list[str]:
    """Matn faylidan ro'yxat o'qiydi (# bilan boshlangan qatorlar izoh)."""
    path = BASE / name
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lstrip("@")
        if line:
            out.append(line)
    return out


def read_list_raw(name: str) -> list[str]:
    """URL ro'yxati: izoh ' #' (bo'shliq + #) bilan boshlanadi."""
    path = BASE / name
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(re.split(r"\s+#", line, 1)[0].strip())
    return out


def normalize(text: str) -> str:
    return re.sub(r"[‘’ʻʼ`]", "'", text).lower()


def load_seen() -> dict | None:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def save_seen(seen: dict):
    cutoff = time.time() - 10 * 86400  # 10 kundan eskilarini o'chirish
    seen = {k: v for k, v in seen.items() if v > cutoff}
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")


def send(text: str):
    for chat_id in CHAT_IDS:
        for _ in range(3):
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": "true"},
                timeout=20,
            )
            if r.status_code == 429:  # Telegram "sekinroq" desa
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
                continue
            if not r.ok:
                print("Yuborishda xato:", chat_id, r.text[:200])
            break
        time.sleep(0.3)


def send_long(text: str):
    while text:
        cut = text.rfind("\n", 0, 4000) if len(text) > 4000 else len(text)
        if cut <= 0:
            cut = 4000
        send(text[:cut])
        text = text[cut:].lstrip("\n")


# ---------------------------------------------------------------- o'zbekcha qisqa mazmun

GEMINI_AUTH: dict = {}   # qaysi avtorizatsiya usuli ishlagani eslab qolinadi

SUMMARY_PROMPT = """Sen yangiliklar muharririsan. Quyidagi JSON ro'yxatdagi har bir xabarni o'qi.

1) "rel" ni QATTIQ qoidaga ko'ra aniqla. rel = true FAQAT agar davlat rahbarining
   (prezident, bosh vazir, qirol, amir, kansler) O'ZI shaxsan qatnashgan bo'lsa:
   - xorijiy davlatga tashrif yoki xorijiy mehmonni qabul qilish
   - boshqa davlat rahbari bilan uchrashuv, muzokara, telefon suhbati
   - sammit, forum, xalqaro anjumanda qatnashish
   - nutq, murojaat, bayonot, matbuot anjumani, intervyu
   - xalqaro kelishuv yoki muhim farmon/qonunni imzolash
   rel = false bo'lgan hollar (bularni tashlab yuborish kerak):
   - tabriknoma, ta'ziyanoma, tug'ilgan kun yoki bayram tabriklari
   - vazir, elchi yoki boshqa amaldorlarning uchrashuvlari (rahbarning o'zi yo'q bo'lsa)
   - madaniyat, sport, ob-havo, iqtisodiy statistika, reklama
   - oddiy kadr tayinlovlari, mukofotlash, ichki xo'jalik masalalari
   - faqat rasm yoki video tavsifi, mazmuni yo'q post
   Ikkilansang, false qo'y.

2) rel = true bo'lsa, o'zbek tilida (lotin yozuvida) 1-2 gaplik qisqa mazmun yoz:
   kim, qayerda, kim bilan, nima qildi yoki nima dedi. Faqat matnda bor faktlarni yoz.

Har bir xabar uchun JSON obyekt qaytar:
{"i": xabar raqami,
 "flag": "rahbar davlatining bayroq emojisi",
 "who": "Lavozimi va ismi, masalan: Rossiya Prezidenti Putin",
 "uz": "o'zbekcha qisqa mazmun (rel=false bo'lsa bo'sh)",
 "rel": true yoki false}

Faqat JSON massiv qaytar, boshqa hech narsa yozma.

Xabarlar:
"""


def _gemini_call(batch: list[dict]) -> dict[int, dict] | None:
    data = json.dumps(batch, ensure_ascii=False)
    body = {"contents": [{"role": "user", "parts": [{"text": SUMMARY_PROMPT + data}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    for model in GEMINI_MODELS:
        for attempt in range(3):
            # Kalit turiga qarab sarlavha: avval x-goog-api-key, ishlamasa Bearer
            if attempt == 0 or not GEMINI_AUTH.get("bearer"):
                headers = {"x-goog-api-key": GEMINI_KEY}
            else:
                headers = {"Authorization": f"Bearer {GEMINI_KEY}"}
            if GEMINI_AUTH.get("bearer") and attempt == 0:
                headers = {"Authorization": f"Bearer {GEMINI_KEY}"}
            headers["Content-Type"] = "application/json"
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers=headers, json=body, timeout=120)
            except Exception as e:
                print("Gemini ulanish xatosi:", e)
                break
            if r.status_code in (400, 401, 403) and not GEMINI_AUTH.get("bearer") \
                    and "x-goog-api-key" in headers and attempt < 2:
                GEMINI_AUTH["bearer"] = True               # boshqa usulni sinab ko'ramiz
                continue
            if r.status_code == 429 and attempt < 2:   # limit — biroz kutib qayta urinish
                time.sleep(30)
                continue
            if not r.ok:
                print(f"Gemini xatosi ({model}):", r.status_code, r.text[:300])
                break
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
                text = re.sub(r"^```(?:json)?|```$", "", text).strip()
                return {int(o["i"]): o for o in json.loads(text) if "i" in o}
            except Exception as e:
                print("Gemini javobini o'qib bo'lmadi:", e)
                break
    return None


def summarize(items: list[dict]) -> dict[int, dict]:
    """items: [{"manba":..., "matn":...}] -> {indeks: {"flag","who","uz","rel"}}"""
    if not GEMINI_KEY or not items:
        return {}
    out = {}
    for start in range(0, len(items), 30):
        batch = [{"i": start + j, "manba": it["manba"], "matn": it["matn"][:1500]}
                 for j, it in enumerate(items[start:start + 30])]
        res = _gemini_call(batch)
        if res:
            out.update(res)
    return out


STRONG_WORDS = [
    "uchrashdi", "uchrashuv", "muzokara", "tashrif", "nutq", "murojaat", "bayonot", "sammit",
    "imzoladi", "qabul qildi", "telefon orqali",
    "встреч", "переговор", "визит", "выступ", "обращени", "заявлени", "саммит", "подписал",
    "принял", "телефонный разговор", "пресс-конференц",
    "met with", "meets", "talks", "visit", "speech", "address", "remarks", "statement",
    "summit", "signed", "press conference", "phone call", "received",
    "görüş", "ziyaret", "konuşma", "zirve", "imzala",
]


def strong_match(text: str) -> bool:
    t = normalize(text)
    return any(w in t for w in STRONG_WORDS)


def translate_uz(text: str) -> str:
    """Zaxira: Gemini ishlamasa, Google Translate orqali o'zbekchaga tarjima."""
    text = text.strip()
    if not text:
        return text
    short = text[:400]
    m = re.match(r"(.+?[.!?])(\s|$)", short, re.S)   # birinchi 1 gap
    if m and len(m.group(1)) > 40:
        short = m.group(1)
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "auto", "tl": "uz", "dt": "t", "q": short},
                         timeout=20)
        return "".join(seg[0] for seg in r.json()[0] if seg and seg[0])
    except Exception:
        return short


# ---------------------------------------------------------------- Telegram kanallar

def fetch_channel(username: str) -> list[dict]:
    """t.me/s/<kanal> ochiq sahifasidan so'nggi ~20 postni oladi."""
    r = requests.get(f"https://t.me/s/{username}", headers=UA, timeout=25)
    if r.status_code != 200 or "tgme_widget_message" not in r.text:
        raise RuntimeError("ochiq sahifa yo'q")
    soup = BeautifulSoup(r.text, "html.parser")
    title_el = soup.select_one(".tgme_channel_info_header_title")
    title = title_el.get_text(strip=True) if title_el else username
    posts = []
    for m in soup.select("div.tgme_widget_message[data-post]"):
        text_el = m.select_one(".tgme_widget_message_text")
        posts.append({
            "id": "tg:" + m["data-post"].lower(),
            "post": m["data-post"],
            "title": title,
            "text": text_el.get_text("\n", strip=True) if text_el else "",
        })
    return posts


def format_post(p: dict, info: dict) -> str:
    url = p.get("link") or f"https://t.me/{p['post']}"
    link = f'<a href="{html.escape(url)}">asl post</a>'
    src = f"📢 {html.escape(p['title'])} · {link}"
    if not p["text"]:
        return f"📷 <i>Rasm yoki video post</i>\n\n{src}"
    uz = info.get("uz") or translate_uz(p["text"])
    who = info.get("who", "").strip()
    head = f"{info.get('flag', '')} <b>{html.escape(who)}</b>\n".lstrip() if who else ""
    return f"{head}{html.escape(uz)}\n\n{src}"


# ---------------------------------------------------------------- saytlar RSS lentalari

def fetch_rss(url: str) -> tuple[str, list[dict]]:
    """Rasmiy saytning RSS lentasidan so'nggi xabarlarni oladi."""
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    feed = feedparser.parse(r.text)
    if not feed.entries:
        raise RuntimeError("lenta bo'sh yoki RSS emas")
    title = feed.feed.get("title", url)
    items = []
    for e in feed.entries[:20]:
        link = e.get("link", "")
        summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True)
        text = (e.get("title", "").strip() + ". " + summary).strip(". ")
        items.append({"id": "rss:" + (link or e.get("id", "") or e.get("title", "")),
                      "post": "", "link": link, "title": title, "text": text[:2000],
                      "leader": True})
    return title, items


# ---------------------------------------------------------------- Google News

def fetch_google() -> list[dict]:
    items, titles = [], set()
    for q in GOOGLE_QUERIES:
        url = ("https://news.google.com/rss/search?q=" + quote_plus(q + " when:1d")
               + "&hl=en-US&gl=US&ceid=US:en")
        try:
            feed = feedparser.parse(requests.get(url, headers=UA, timeout=25).text)
        except Exception as e:
            print("Google xatosi:", q, e)
            continue
        for e in feed.entries:
            title = e.get("title", "").strip()
            src = e.get("source", {}).get("title", "") if e.get("source") else ""
            if src and title.endswith(" - " + src):
                title = title[: -len(" - " + src)]
            key = title.lower()[:80]
            if key in titles:
                continue
            titles.add(key)
            items.append({"id": "gn:" + key, "title": title, "source": src,
                          "link": e.get("link", "")})
    return items


# ---------------------------------------------------------------- asosiy

def run_once(do_google: bool = True):
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}
    now = time.time()

    leader_channels = read_list("rahbar_kanallari.txt")   # hamma postlari yuboriladi
    news_channels = read_list("yangilik_kanallari.txt")   # faqat kalit so'z bo'lsa
    keywords = [normalize(k) for k in read_list("kalit_sozlar.txt")]

    new_posts, failed = [], []
    added_ok, added_bad = [], []   # ro'yxatga yangi qo'shilgan kanallar
    for ch in leader_channels + news_channels:
        ok_key, bad_key = "ch:" + ch.lower(), "chfail:" + ch.lower()
        try:
            posts = fetch_channel(ch)
        except Exception as e:
            failed.append(f"@{ch}")
            print("Kanal o'qilmadi:", ch, e)
            if bad_key not in seen and not first_run:
                added_bad.append(f"@{ch}")
            seen[bad_key] = now          # bir marta xabar beriladi
            continue
        seen.pop(bad_key, None)
        is_new_channel = ok_key not in seen
        seen[ok_key] = now
        if is_new_channel and not first_run:
            added_ok.append(f"@{ch}")
        is_leader = ch in leader_channels
        for p in posts:
            p["leader"] = is_leader
            if p["id"] in seen:
                continue
            seen[p["id"]] = now
            if is_new_channel:
                continue                 # yangi kanalning eski postlari yuborilmaydi
            found = [k for k in keywords if k in normalize(p["text"])]
            if is_leader or found:
                new_posts.append((p, found))
        time.sleep(1)

    for url in read_list_raw("rss_manbalar.txt"):
        ok_key, bad_key = "src:" + url, "srcfail:" + url
        try:
            title, items = fetch_rss(url)
        except Exception as e:
            failed.append(url)
            print("RSS o'qilmadi:", url, e)
            if bad_key not in seen and not first_run:
                added_bad.append(url)
            seen[bad_key] = now
            continue
        seen.pop(bad_key, None)
        is_new_src = ok_key not in seen
        seen[ok_key] = now
        if is_new_src and not first_run:
            added_ok.append(title)
        for it in items:
            if it["id"] in seen:
                continue
            seen[it["id"]] = now
            if not is_new_src:
                new_posts.append((it, []))

    news = [n for n in fetch_google() if n["id"] not in seen] if do_google else []
    for n in news:
        seen[n["id"]] = now

    if first_run:
        msg = (f"✅ Bot ishga tushdi!\n\nKanallar: {len(leader_channels) + len(news_channels)} ta, "
               f"o'qildi: {len(leader_channels) + len(news_channels) - len(failed)} ta.")
        if failed:
            msg += ("\n\n⚠️ Bu kanallarni o'qib bo'lmadi (nomi xato yoki ochiq sahifasi yo'q):\n"
                    + "\n".join(failed))
        msg += "\n\nEndi yangi xabarlar chiqishi bilan yuborib turaman."
        send(html.escape(msg))
    else:
        if added_ok or added_bad:
            msg = ""
            if added_ok:
                msg += "📡 Yangi kanallar ulandi:\n" + "\n".join(added_ok)
            if added_bad:
                msg += ("\n\n⚠️ Bu kanallarni o'qib bo'lmadi (nomi xato yoki ochiq sahifasi yo'q):\n"
                        + "\n".join(added_bad))
            send(html.escape(msg.strip()))
        posts = new_posts[-MAX_POSTS_PER_RUN:]
        news = news[:40]
        items = ([{"manba": p["title"], "matn": p["text"]} for p, _ in posts]
                 + [{"manba": n["source"], "matn": n["title"]} for n in news])
        infos = summarize(items)

        for k, (p, found) in enumerate(posts):
            info = infos.get(k)
            if not p["text"]:
                continue                     # matnsiz (faqat rasm/video) post
            if info is not None:
                if info.get("rel") is not True:
                    continue                 # mayda yangilik — tashlanadi
            elif not strong_match(p["text"]):
                continue                     # Gemini ishlamasa: kalit so'z bo'yicha
            send(format_post(p, info or {}))

        lines = []
        for k, n in enumerate(news, start=len(posts)):
            info = infos.get(k)
            if info is not None:
                if info.get("rel") is not True:
                    continue
            elif not strong_match(n["title"]):
                continue
            info = info or {}
            uz = info.get("uz") or translate_uz(n["title"])
            who = info.get("who", "").strip()
            head = f"{info.get('flag', '')} <b>{html.escape(who)}</b>: " if who else "• "
            lines.append(f'{head}{html.escape(uz)} <a href="{html.escape(n["link"])}">'
                         f'[{html.escape(n["source"] or "manba")}]</a>')
        if lines:
            send_long("🌍 <b>Dunyo rahbarlari: yangiliklar</b>\n\n" + "\n\n".join(lines))

    save_seen(seen)
    print(f"Tayyor. Postlar: {len(new_posts)}, yangiliklar: {len(news)}, xato kanallar: {failed}")


# ---------------------------------------------------------------- uzluksiz rejim

LOOP_MINUTES = int(os.getenv("LOOP_MINUTES", "0") or 0)   # 0 = bir marta ishlab to'xtaydi
CHECK_EVERY_SEC = 60        # manbalarni tekshirish oralig'i
GOOGLE_EVERY = 5            # Google News har 5-tekshiruvda (bloklanmaslik uchun)
SYNC_EVERY = 5              # holatni GitHub'ga har 5-tekshiruvda saqlash


def git_sync():
    """seen.json ni saqlaydi va siz GitHub'da o'zgartirgan ro'yxatlarni tortib oladi."""
    cmd = ('git add seen.json && (git diff --cached --quiet || git commit -q -m "holat yangilandi"); '
           'git pull -q --rebase && git push -q')
    try:
        subprocess.run(cmd, shell=True, cwd=BASE, timeout=120)
    except Exception as e:
        print("git sync xatosi:", e)


def main():
    if not LOOP_MINUTES:
        run_once()
        return
    end = time.time() + LOOP_MINUTES * 60
    i = 0
    try:
        while time.time() < end:
            started = time.time()
            try:
                run_once(do_google=(i % GOOGLE_EVERY == 0))
            except Exception as e:
                print("Tekshiruvda xato:", e)
            i += 1
            if i % SYNC_EVERY == 0:
                git_sync()
            time.sleep(max(5, CHECK_EVERY_SEC - (time.time() - started)))
    finally:
        git_sync()


if __name__ == "__main__":
    main()
