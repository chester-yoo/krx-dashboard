"""
네이버 금융에서 업종(산업) 분류를 종목코드로 조회해 로컬 캐시(data/industry.json)에 저장한다.
KRX Open API에는 업종 분류 데이터가 없어서 네이버 금융 공개 페이지를 사용한다.

사용법
  python industry.py update   data/history.json에 등장하는 종목 중 캐시에 없는 것만 새로 조회
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
HISTORY_PATH = BASE_DIR / "data" / "history.json"
CACHE_PATH = BASE_DIR / "data" / "industry.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
INDUSTRY_RE = re.compile(r'sise_group_detail\.naver\?type=upjong&no=(\d+)">([^<]+)</a>')


def load_cache():
    if not CACHE_PATH.exists():
        return {}
    with open(CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0, separators=(",", ":"))


def fetch_industry(code):
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    if resp.status_code != 200:
        return None
    text = resp.content.decode("utf-8", errors="replace")
    m = INDUSTRY_RE.search(text)
    return m.group(2) if m else None


def update_missing(codes, cache=None, save=True):
    """캐시에 없는 코드만 네이버에서 조회해 채운다. codes: iterable of 6자리 종목코드"""
    if cache is None:
        cache = load_cache()
    missing = [c for c in dict.fromkeys(codes) if c not in cache]
    if not missing:
        return cache
    print(f"[industry] 업종 조회 대상: {len(missing)}개")
    for i, code in enumerate(missing):
        try:
            name = fetch_industry(code)
            cache[code] = name or ""
        except Exception as e:
            print(f"[industry]  {code} 오류: {e}")
            cache[code] = ""
        if (i + 1) % 50 == 0:
            if save:
                save_cache(cache)
            print(f"[industry]  {i + 1}/{len(missing)} 진행")
        time.sleep(0.15)
    if save:
        save_cache(cache)
    print("[industry] 완료")
    return cache


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "update":
        print(__doc__)
        sys.exit(1)
    if not HISTORY_PATH.exists():
        print("data/history.json이 없습니다. 먼저 collect.py를 실행하세요.")
        sys.exit(1)
    history = json.load(open(HISTORY_PATH, encoding="utf-8"))
    codes = sorted({r["code"] for r in history})
    update_missing(codes)
