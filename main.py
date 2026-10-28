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


def format_post(p: dict, found: list[str]) -> str:
    username = p["post"].split("/")[0]
    body = html.escape(p["text"][:3300]) + ("…" if len(p["text"]) > 3300 else "")
    if not body:
        body = "<i>(matnsiz post: rasm yoki video)</i>"
    keys = f"🔑 <i>{html.escape(', '.join(found[:5]))}</i>\n" if found else ""
    return (f"📢 <b>{html.escape(p['title'])}</b> (@{username})\n{keys}\n{body}\n\n"
            f'🔗 <a href="https://t.me/{p["post"]}">Postni ochish</a>')


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

def main():
    seen = load_seen()
    first_run = seen is None
    seen = seen or {}
    now = time.time()

    leader_channels = read_list("rahbar_kanallari.txt")   # hamma postlari yuboriladi
    news_channels = read_list("yangilik_kanallari.txt")   # faqat kalit so'z bo'lsa
    keywords = [normalize(k) for k in read_list("kalit_sozlar.txt")]

    new_posts, failed = [], []
    for ch in leader_channels + news_channels:
        try:
            posts = fetch_channel(ch)
        except Exception as e:
            failed.append(f"@{ch}")
            print("Kanal o'qilmadi:", ch, e)
            continue
        is_leader = ch in leader_channels
        for p in posts:
            if p["id"] in seen:
                continue
            seen[p["id"]] = now
            found = [k for k in keywords if k in normalize(p["text"])]
            if is_leader or found:
                new_posts.append((p, found))
        time.sleep(1)

    news = [n for n in fetch_google() if n["id"] not in seen]
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
        for p, found in new_posts[-MAX_POSTS_PER_RUN:]:
            send(format_post(p, found))
        if news:
            lines = [f'• <a href="{html.escape(n["link"])}">{html.escape(n["title"])}</a>'
                     f' <i>({html.escape(n["source"])})</i>' for n in news[:25]]
            send_long(f"🌍 <b>Yangiliklar: {len(news)} ta yangi</b>\n\n" + "\n".join(lines))

    save_seen(seen)
    print(f"Tayyor. Postlar: {len(new_posts)}, yangiliklar: {len(news)}, xato kanallar: {failed}")


if __name__ == "__main__":
    main()
