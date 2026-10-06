"""
일자별 시가총액 원천 데이터 저장소
===================================
data/history/YYYYMM.json 월별 파일에 누적 저장한다.
과거 월 파일은 더 이상 바뀌지 않으므로 git 이력이 가볍고, 단일 파일이 100MB(GitHub 한도)를
넘지 않는다. 한 달 분량은 약 10MB 수준.

예전 단일 파일(data/history.json)이 남아 있으면 migrate()로 월별 파일로 분할한다.

사용법
  python store.py migrate    data/history.json -> data/history/YYYYMM.json 분할 후 원본 삭제
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SHARD_DIR = BASE_DIR / "data" / "history"
LEGACY_PATH = BASE_DIR / "data" / "history.json"


def shard_path(yyyymm):
    return SHARD_DIR / f"{yyyymm}.json"


def _dump(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r["date"], r["code"]))
    # 한 줄에 한 행: 일별 추가분이 git diff에서 줄 단위로 잡혀 저장소 증가폭이 작다
    with open(path, "w", encoding="utf-8") as f:
        f.write("[\n")
        f.write(",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows))
        f.write("\n]\n")


def load_month(yyyymm):
    path = shard_path(yyyymm)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_month(yyyymm, rows):
    _dump(shard_path(yyyymm), rows)


def months():
    if not SHARD_DIR.exists():
        return []
    return sorted(p.stem for p in SHARD_DIR.glob("*.json"))


def load_all():
    rows = []
    for m in months():
        rows.extend(load_month(m))
    return rows


def all_codes():
    return sorted({r["code"] for r in load_all()})


def migrate():
    if not LEGACY_PATH.exists():
        print("data/history.json 없음 - 이미 분할된 상태")
        return
    with open(LEGACY_PATH, "r", encoding="utf-8") as f:
        legacy = json.load(f)
    by_month = {}
    for r in legacy:
        by_month.setdefault(r["date"][:6], []).append(r)
    for m, rows in by_month.items():
        existing = {(r["date"], r["code"]) for r in load_month(m)}
        merged = load_month(m) + [r for r in rows if (r["date"], r["code"]) not in existing]
        save_month(m, merged)
        print(f"{m}: {len(merged)}행")
    LEGACY_PATH.unlink()
    print(f"분할 완료: {len(by_month)}개 월 파일, 원본 history.json 삭제")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "migrate":
        migrate()
    else:
        print(__doc__)
        sys.exit(1)
