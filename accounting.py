#!/usr/bin/env python3
"""
全力会計 SQLite中間形式変換スクリプト

使い方:
  python3 accounting.py import    # 入力ファイル/ のCSVをDBに取り込む
  python3 accounting.py export    # journalsから全力会計CSVを出力
  python3 accounting.py status    # DB統計を表示

オプション:
  import  --force              重複チェックを無視して再取り込み
  import  --include-processed  処理済み/ フォルダも対象に含める
  export  --month YYYYMM       特定月のみ出力

仕訳 (classify) はClaude Codeとのチャットで行う。
"""

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import unicodedata
import urllib.request
from datetime import datetime, date

# frankfurter.app TTMレートのインメモリキャッシュ (date+currency → rate)
_ttm_cache: dict[str, float] = {}


def _fetch_ttm_rate(date_str: str, from_ccy: str) -> float | None:
    """取引日の公式為替レート(TTM近似)をfrankfurter.appから取得する。
    取得失敗時はNoneを返し、処理を止めない。"""
    if from_ccy == "JPY":
        return 1.0
    key = f"{date_str}:{from_ccy}"
    if key in _ttm_cache:
        return _ttm_cache[key]
    try:
        url = f"https://api.frankfurter.app/{date_str}?from={from_ccy}&to=JPY"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        rate = data["rates"]["JPY"]
        _ttm_cache[key] = rate
        return rate
    except Exception:
        return None

# ── 設定 ─────────────────────────────────────────────────────────────────────
# PayPay明細で代表者への振込をスキップ判定するためのカタカナ氏名（姓・名それぞれ部分一致）
# 例: 「代表者セイ 代表者メイ」 → ["代表者セイ", "代表者メイ"]
REPRESENTATIVE_KATAKANA = ["代表者セイ", "代表者メイ"]

# 役員報酬と相殺する月次金額（PayPayでの立替相殺など）
SALARY_OFFSET_AMOUNT = 1_200_000

# ── 定数 ─────────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "accounting.db")
BASE    = os.path.dirname(__file__)

ZENRYOKU_HEADER = [
    "日付", "伝票No.", "借方勘定科目", "借方補助科目", "借方金額", "借方税額",
    "借方税区分", "借方内外区分", "借方Noインボイス", "借方タグ",
    "貸方勘定科目", "貸方補助科目", "貸方金額", "貸方税額",
    "貸方税区分", "貸方内外区分", "貸方Noインボイス", "貸方タグ",
    "摘要", "決算仕訳フラグ", "開始残高フラグ", "メモ",
]

ACCOUNT_CODE = {
    "paypay":    "01",
    "epos":      "02",
    "sbi":       "06",
    "tatekaeri": "08",
}

# ── スキーマ ──────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS import_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    filename    TEXT    NOT NULL,
    imported_at TEXT    NOT NULL,
    tx_count    INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_import_log ON import_log(filename);

CREATE TABLE IF NOT EXISTS transactions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT    NOT NULL,
    source_file      TEXT    NOT NULL,
    tx_date          TEXT    NOT NULL,  -- YYYY-MM-DD
    description      TEXT    NOT NULL,  -- 生の摘要（未normalize）
    direction        TEXT    NOT NULL CHECK(direction IN ('in','out')),
    amount_jpy       INTEGER NOT NULL DEFAULT 0,
    amount_foreign   REAL,
    foreign_currency TEXT,
    exchange_rate    REAL,
    tx_type          TEXT,
    is_overseas      INTEGER NOT NULL DEFAULT 0,
    extra            TEXT,              -- JSON: ソース固有の追加フィールド
    imported_at      TEXT    NOT NULL,
    skip             INTEGER NOT NULL DEFAULT 0,
    skip_reason      TEXT
);

CREATE TABLE IF NOT EXISTS journals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id   INTEGER REFERENCES transactions(id),
    seq              INTEGER NOT NULL DEFAULT 1,
    tx_date          TEXT    NOT NULL,
    dr_account       TEXT    NOT NULL,
    dr_sub           TEXT    NOT NULL DEFAULT '',
    dr_amount        INTEGER NOT NULL,
    dr_tax           INTEGER NOT NULL DEFAULT 0,
    dr_tax_type      TEXT    NOT NULL,
    dr_inout         TEXT    NOT NULL DEFAULT '',
    dr_invoice       INTEGER NOT NULL DEFAULT 0,
    cr_account       TEXT    NOT NULL,
    cr_sub           TEXT    NOT NULL DEFAULT '',
    cr_amount        INTEGER NOT NULL,
    cr_tax           INTEGER NOT NULL DEFAULT 0,
    cr_tax_type      TEXT    NOT NULL,
    cr_inout         TEXT    NOT NULL DEFAULT '',
    cr_invoice       INTEGER NOT NULL DEFAULT 0,
    description      TEXT    NOT NULL,
    memo             TEXT    NOT NULL DEFAULT '',
    is_uncertain     INTEGER NOT NULL DEFAULT 0,
    uncertain_reason TEXT,
    classified_at    TEXT    NOT NULL
);
"""


# ── DB初期化 ──────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# ── ユーティリティ ────────────────────────────────────────────────────────────
def normalize_desc(s: str) -> str:
    """摘要記号制限に従って正規化する（CLAUDE.md規定順）"""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("　", " ")
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("-", " ")
    s = re.sub(r"[^\w\s()/.,぀-ヿ一-鿿＀-￯ー]", " ", s)
    s = re.sub(r" +", " ", s).strip()
    return s


def calc_tax(amount: int) -> int:
    return round(amount / 11)


def parse_amount(s: str) -> int:
    return int(s.replace(",", "").replace("−", "-").replace("–", "-").strip() or "0")


def now_iso() -> str:
    return datetime.now().isoformat()


def _already_imported(conn: sqlite3.Connection, filename: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM import_log WHERE filename=?", (filename,)
    ).fetchone())


def _insert_txns(conn: sqlite3.Connection, source: str, filename: str, txns: list[dict]):
    ts = now_iso()
    conn.executemany(
        """INSERT INTO transactions
           (source, source_file, tx_date, description, direction,
            amount_jpy, amount_foreign, foreign_currency, exchange_rate,
            tx_type, is_overseas, extra, imported_at, skip, skip_reason)
           VALUES (:source,:source_file,:tx_date,:description,:direction,
                   :amount_jpy,:amount_foreign,:foreign_currency,:exchange_rate,
                   :tx_type,:is_overseas,:extra,:imported_at,:skip,:skip_reason)""",
        [{
            "source":          source,
            "source_file":     filename,
            "tx_date":         t["tx_date"],
            "description":     t["description"],
            "direction":       t.get("direction", "out"),
            "amount_jpy":      t.get("amount_jpy", 0),
            "amount_foreign":  t.get("amount_foreign"),
            "foreign_currency":t.get("foreign_currency"),
            "exchange_rate":   t.get("exchange_rate"),
            "tx_type":         t.get("tx_type"),
            "is_overseas":     int(t.get("is_overseas", False)),
            "extra":           json.dumps(t.get("extra", {}), ensure_ascii=False),
            "imported_at":     ts,
            "skip":            int(t.get("skip", False)),
            "skip_reason":     t.get("skip_reason"),
        } for t in txns]
    )
    conn.execute(
        "INSERT INTO import_log (source,filename,imported_at,tx_count) VALUES (?,?,?,?)",
        (source, filename, ts, len(txns)),
    )
    conn.commit()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IMPORT STAGE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def import_epos(conn: sqlite3.Connection, path: str, force: bool = False) -> int:
    filename = os.path.basename(path)
    if not force and _already_imported(conn, filename):
        print(f"  [個人クレカ] skip (already imported): {filename}")
        return 0

    with open(path, encoding="shift_jis") as f:
        content = f.read()

    txns = []
    for line in content.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        kind = parts[0]
        if kind not in ("ショッピング", "その他ご利用"):
            continue
        try:
            date_str = re.sub(r"[年月]", "/", parts[1]).replace("日", "").strip()
            dt = datetime.strptime(date_str, "%Y/%m/%d").date()
            merchant       = parts[2]
            content_field  = parts[3]
            amt = parse_amount(parts[4])
            if amt == 0:
                continue
            remarks = parts[7] if len(parts) > 7 else ""
            txns.append({
                "tx_date":     dt.strftime("%Y-%m-%d"),
                "description": merchant if kind == "ショッピング" else content_field,
                "direction":   "out",
                "amount_jpy":  abs(amt),
                "tx_type":     kind,
                "extra":       {"merchant": merchant, "content": content_field, "remarks": remarks},
            })
        except Exception:
            continue

    if force and _already_imported(conn, filename):
        old_ids = [r[0] for r in conn.execute(
            "SELECT id FROM transactions WHERE source_file=?", (filename,)).fetchall()]
        if old_ids:
            ph = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM journals WHERE transaction_id IN ({ph})", old_ids)
        conn.execute("DELETE FROM transactions WHERE source_file=?", (filename,))
        conn.execute("DELETE FROM import_log WHERE filename=?", (filename,))
        conn.commit()

    _insert_txns(conn, "epos", filename, txns)
    print(f"  [個人クレカ] {filename}: {len(txns)} 件")
    return len(txns)


def import_paypay(conn: sqlite3.Connection, path: str, force: bool = False) -> int:
    filename = os.path.basename(path)
    if not force and _already_imported(conn, filename):
        print(f"  [PayPay] skip (already imported): {filename}")
        return 0

    with open(path, encoding="shift_jis") as f:
        reader = csv.reader(f)
        raw_rows = list(reader)

    txns = []
    for parts in raw_rows[1:]:
        if len(parts) < 10:
            continue
        try:
            yr, mo, dy = int(parts[0]), int(parts[1]), int(parts[2])
            dt       = date(yr, mo, dy)
            desc_raw = parts[7].strip()
            pay_str  = parts[8].strip().replace(",", "")
            recv_str = parts[9].strip().replace(",", "")
            pay_amt  = int(pay_str)  if pay_str  else 0
            recv_amt = int(recv_str) if recv_str else 0
        except Exception:
            continue

        is_taki_offset = (
            all(part in desc_raw for part in REPRESENTATIVE_KATAKANA) and
            (pay_amt == SALARY_OFFSET_AMOUNT or recv_amt == SALARY_OFFSET_AMOUNT)
        )

        if recv_amt > 0:
            txns.append({
                "tx_date":     dt.strftime("%Y-%m-%d"),
                "description": desc_raw,
                "direction":   "in",
                "amount_jpy":  recv_amt,
                "tx_type":     "入金",
                "skip":        is_taki_offset,
                "skip_reason": "役員報酬と相殺" if is_taki_offset else None,
            })
        if pay_amt > 0:
            txns.append({
                "tx_date":     dt.strftime("%Y-%m-%d"),
                "description": desc_raw,
                "direction":   "out",
                "amount_jpy":  pay_amt,
                "tx_type":     "出金",
                "skip":        is_taki_offset,
                "skip_reason": "役員報酬と相殺" if is_taki_offset else None,
            })

    if force and _already_imported(conn, filename):
        old_ids = [r[0] for r in conn.execute(
            "SELECT id FROM transactions WHERE source_file=?", (filename,)).fetchall()]
        if old_ids:
            ph = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM journals WHERE transaction_id IN ({ph})", old_ids)
        conn.execute("DELETE FROM transactions WHERE source_file=?", (filename,))
        conn.execute("DELETE FROM import_log WHERE filename=?", (filename,))
        conn.commit()

    _insert_txns(conn, "paypay", filename, txns)
    print(f"  [PayPay] {filename}: {len(txns)} 件")
    return len(txns)


def import_wise(conn: sqlite3.Connection, path: str, force: bool = False) -> int:
    filename = os.path.basename(path)
    if not force and _already_imported(conn, filename):
        print(f"  [Wise] skip (already imported): {filename}")
        return 0

    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        raw = list(reader)

    txns = []
    for r in raw:
        txid    = r.get("ID", "").strip()
        status  = r.get("ステータス", "").strip()
        kind    = r.get("送金の種類", "").strip()
        created = r.get("作成日", "").strip()
        cat     = r.get("カテゴリ", "").strip()
        fee_str = r.get("送金手数料", "0") or "0"
        fee_ccy = r.get("送金手数料の通貨", "JPY").strip()
        send_str = r.get("送金額（手数料差し引き後）", "0") or "0"
        send_ccy = r.get("送金元通貨", "JPY").strip()
        recv_str = r.get("受取額（手数料差し引き後）", "0") or "0"
        recv_ccy = r.get("受取通貨", "JPY").strip()
        dest     = r.get("送金先", "").strip()

        if status == "CANCELLED":
            continue
        try:
            dt = datetime.strptime(created[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        try:
            fee      = float(fee_str.replace(",", ""))
            send_amt = float(send_str.replace(",", ""))
            recv_amt = float(recv_str.replace(",", ""))
        except Exception:
            continue

        if "BALANCE_TRANSACTION" in txid and kind == "NEUTRAL" and send_ccy == "JPY":
            fee_jpy  = int(fee) if fee_ccy == "JPY" else 0
            net_jpy  = int(send_amt)
            eff_rate = round(net_jpy / recv_amt, 4) if recv_amt else None
            ttm_rate = _fetch_ttm_rate(dt.strftime("%Y-%m-%d"), recv_ccy)
            txns.append({
                "tx_date":        dt.strftime("%Y-%m-%d"),
                "description":    f"Wise {recv_ccy}変換 → {dest}",
                "direction":      "out",
                "amount_jpy":     net_jpy,
                "amount_foreign": recv_amt,
                "foreign_currency": recv_ccy,
                "exchange_rate":  eff_rate,
                "tx_type":        "BALANCE_TRANSACTION",
                "extra": {
                    "txid":         txid,
                    "fee_jpy":      fee_jpy,
                    "foreign_amt":  recv_amt,
                    "dest":         dest,
                    "effective_rate": eff_rate,
                    "ttm_rate":     ttm_rate,
                },
            })

        elif "TRANSFER" in txid and kind == "OUT" and cat != "チャージ" and send_ccy == "JPY":
            fee_jpy  = int(fee) if fee_ccy == "JPY" else 0
            net_jpy  = int(send_amt) - fee_jpy
            eff_rate = round(net_jpy / recv_amt, 4) if recv_amt else None
            ttm_rate = _fetch_ttm_rate(dt.strftime("%Y-%m-%d"), recv_ccy)
            txns.append({
                "tx_date":        dt.strftime("%Y-%m-%d"),
                "description":    f"Wise 送金 {dest}",
                "direction":      "out",
                "amount_jpy":     net_jpy,
                "amount_foreign": recv_amt,
                "foreign_currency": recv_ccy,
                "exchange_rate":  eff_rate,
                "tx_type":        "TRANSFER_OUT",
                "extra": {
                    "txid":         txid,
                    "fee_jpy":      fee_jpy,
                    "foreign_amt":  recv_amt,
                    "dest":         dest,
                    "effective_rate": eff_rate,
                    "ttm_rate":     ttm_rate,
                },
            })

        elif "TRANSFER" in txid and kind == "IN" and cat == "チャージ":
            txns.append({
                "tx_date":    dt.strftime("%Y-%m-%d"),
                "description": f"Wise チャージ {dest}",
                "direction":  "in",
                "amount_jpy": int(send_amt),
                "tx_type":    "CHARGE",
                "skip":       True,
                "skip_reason": "PayPay出金と重複",
            })

    if force and _already_imported(conn, filename):
        old_ids = [r[0] for r in conn.execute(
            "SELECT id FROM transactions WHERE source_file=?", (filename,)).fetchall()]
        if old_ids:
            ph = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM journals WHERE transaction_id IN ({ph})", old_ids)
        conn.execute("DELETE FROM transactions WHERE source_file=?", (filename,))
        conn.execute("DELETE FROM import_log WHERE filename=?", (filename,))
        conn.commit()

    _insert_txns(conn, "wise", filename, txns)
    print(f"  [Wise] {filename}: {len(txns)} 件")
    return len(txns)


def import_upsider(conn: sqlite3.Connection, path: str, force: bool = False) -> int:
    filename = os.path.basename(path)
    if not force and _already_imported(conn, filename):
        print(f"  [法人クレカ] skip (already imported): {filename}")
        return 0

    with open(path, encoding="shift_jis") as f:
        reader = csv.DictReader(f)
        raw = list(reader)

    txns = []
    for r in raw:
        try:
            dt  = datetime.strptime(r["取引日"].strip(), "%Y/%m/%d").date()
            merchant = r["利用先"].strip()
            pay_str  = r.get("出金金額", "").strip().replace(",", "")
            recv_str = r.get("入金金額", "").strip().replace(",", "")
            pay_amt  = int(pay_str)  if pay_str  else 0
            recv_amt = int(recv_str) if recv_str else 0
            currency = r.get("通貨", "JPY").strip()

            foreign_amt  = None
            foreign_ccy  = None
            exchange_rate = None
            if currency != "JPY":
                try:
                    foreign_amt  = float(r.get("外貨の金額", "0") or "0")
                    foreign_ccy  = currency
                    exchange_rate = float(r.get("為替レート", "0") or "0") or None
                except Exception:
                    pass

            tx_id  = r.get("決済ID", "").strip()
            memo   = r.get("メモ", "").strip()
            card   = r.get("カード名", "").strip()

            if pay_amt > 0:
                txns.append({
                    "tx_date":        dt.strftime("%Y-%m-%d"),
                    "description":    merchant,
                    "direction":      "out",
                    "amount_jpy":     pay_amt,
                    "amount_foreign": foreign_amt,
                    "foreign_currency": foreign_ccy,
                    "exchange_rate":  exchange_rate,
                    "tx_type":        "出金",
                    "extra":          {"tx_id": tx_id, "memo": memo, "card": card,
                                       "currency": currency},
                })
            elif recv_amt > 0:
                txns.append({
                    "tx_date":        dt.strftime("%Y-%m-%d"),
                    "description":    merchant,
                    "direction":      "in",
                    "amount_jpy":     recv_amt,
                    "amount_foreign": foreign_amt,
                    "foreign_currency": foreign_ccy,
                    "exchange_rate":  exchange_rate,
                    "tx_type":        "入金",
                    "extra":          {"tx_id": tx_id, "memo": memo, "card": card},
                })
        except Exception:
            continue

    if force and _already_imported(conn, filename):
        old_ids = [r[0] for r in conn.execute(
            "SELECT id FROM transactions WHERE source_file=?", (filename,)).fetchall()]
        if old_ids:
            ph = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM journals WHERE transaction_id IN ({ph})", old_ids)
        conn.execute("DELETE FROM transactions WHERE source_file=?", (filename,))
        conn.execute("DELETE FROM import_log WHERE filename=?", (filename,))
        conn.commit()

    _insert_txns(conn, "upsider", filename, txns)
    print(f"  [法人クレカ] {filename}: {len(txns)} 件")
    return len(txns)


def import_sbi(conn: sqlite3.Connection, path: str, force: bool = False) -> int:
    filename = os.path.basename(path)
    if not force and _already_imported(conn, filename):
        print(f"  [SBI] skip (already imported): {filename}")
        return 0

    with open(path, encoding="shift_jis") as f:
        content = f.read()

    txns = []
    lines = content.splitlines()

    header_idx = next(
        (i for i, l in enumerate(lines) if "約定日" in l and "銘柄" in l), None
    )
    if header_idx is not None:
        reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
        for r in reader:
            try:
                date_str = r["約定日"].strip().strip('"')
                date_str = re.sub(r"[年月]", "/", date_str).replace("日", "").strip()
                dt       = datetime.strptime(date_str, "%Y/%m/%d").date()
                brand    = r.get("銘柄", "").strip()
                trade    = r.get("取引", "").strip()
                qty_str  = r.get("約定数量", "").strip()
                price_str = r.get("約定単価", "").strip()
                settle_str = r.get("受渡金額/決済損益", "").strip().replace(",", "")
                if not settle_str:
                    continue
                settle_amt = int(settle_str)
                is_buy = "買" in trade
                direction = "out" if is_buy else "in"
                txns.append({
                    "tx_date":     dt.strftime("%Y-%m-%d"),
                    "description": f"{brand} {trade}",
                    "direction":   direction,
                    "amount_jpy":  abs(settle_amt),
                    "tx_type":     "約定",
                    "extra":       {"trade": trade, "qty": qty_str or "0",
                                    "unit_price": price_str or "0"},
                })
            except Exception:
                continue

    header_idx2 = next(
        (i for i, l in enumerate(lines) if "国内約定日" in l and "受渡金額" in l), None
    )
    if header_idx2 is not None:
        reader2 = csv.DictReader(io.StringIO("\n".join(lines[header_idx2:])))
        for r in reader2:
            try:
                date_str = r["国内約定日"].strip().strip('"')
                date_str = re.sub(r"[年月]", "/", date_str).replace("日", "").strip()
                dt       = datetime.strptime(date_str, "%Y/%m/%d").date()
                brand    = r.get("銘柄名", "").strip()
                trade    = r.get("取引", "").strip()
                qty_str  = r.get("約定数量", "0").strip()
                price_str = r.get("約定単価", "0").strip()
                settle_str = r.get("受渡金額", "").strip().replace(",", "")
                if not settle_str:
                    continue
                settle_amt = int(settle_str)
                is_buy     = "買" in trade
                direction  = "out" if is_buy else "in"
                txns.append({
                    "tx_date":     dt.strftime("%Y-%m-%d"),
                    "description": f"{brand} {trade}",
                    "direction":   direction,
                    "amount_jpy":  abs(settle_amt),
                    "tx_type":     "約定（外貨）",
                    "extra":       {"trade": trade, "qty": qty_str,
                                    "unit_price": price_str},
                })
            except Exception:
                continue

    if force and _already_imported(conn, filename):
        old_ids = [r[0] for r in conn.execute(
            "SELECT id FROM transactions WHERE source_file=?", (filename,)).fetchall()]
        if old_ids:
            ph = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM journals WHERE transaction_id IN ({ph})", old_ids)
        conn.execute("DELETE FROM transactions WHERE source_file=?", (filename,))
        conn.execute("DELETE FROM import_log WHERE filename=?", (filename,))
        conn.commit()

    _insert_txns(conn, "sbi", filename, txns)
    print(f"  [SBI] {filename}: {len(txns)} 件")
    return len(txns)


def _find_csvs(folder: str, include_processed: bool = False) -> list[str]:
    paths = []
    d = os.path.join(BASE, folder)
    if os.path.isdir(d):
        paths.extend(
            os.path.join(d, f)
            for f in sorted(os.listdir(d))
            if f.lower().endswith(".csv")
        )
    if include_processed:
        proc = os.path.join(BASE, folder, "処理済み")
        if os.path.isdir(proc):
            paths.extend(
                os.path.join(proc, f)
                for f in sorted(os.listdir(proc))
                if f.lower().endswith(".csv")
            )
    return paths


def cmd_import(conn: sqlite3.Connection, force: bool = False,
               include_processed: bool = False):
    label = "（処理済み/含む）" if include_processed else ""
    print(f"=== IMPORT {label}===")
    total = 0
    for path in _find_csvs("クレジットカード", include_processed):
        total += import_epos(conn, path, force)
    for path in _find_csvs("銀行", include_processed):
        total += import_paypay(conn, path, force)
    for path in _find_csvs("証券", include_processed):
        total += import_sbi(conn, path, force)
    print(f"→ 合計 {total} 件インポート完了\n")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EXPORT STAGE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _write_zenryoku_csv(path: str, rows: list[list]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    buf = io.StringIO()
    w   = csv.writer(buf, lineterminator="\n")
    w.writerow(ZENRYOKU_HEADER)
    for r in rows:
        w.writerow(r)
    content = buf.getvalue()
    with open(path, "w", encoding="shift_jis", errors="replace") as f:
        f.write(content)
    utf8_path = path.replace("_zenryoku.csv", "_zenryoku_utf8.csv")
    with open(utf8_path, "w", encoding="utf-8") as f:
        f.write(content)
    return utf8_path


def _journal_to_csv_row(j: sqlite3.Row, voucher: str) -> list:
    dt = datetime.strptime(j["tx_date"], "%Y-%m-%d")
    date_str = f"{dt.year}/{dt.month}/{dt.day}"
    return [
        date_str, voucher,
        j["dr_account"], j["dr_sub"], j["dr_amount"], j["dr_tax"],
        j["dr_tax_type"], j["dr_inout"], j["dr_invoice"], "",
        j["cr_account"], j["cr_sub"], j["cr_amount"], j["cr_tax"],
        j["cr_tax_type"], j["cr_inout"], j["cr_invoice"], "",
        j["description"], "", "", j["memo"],
    ]


def cmd_export(conn: sqlite3.Connection, month: str | None = None):
    print("=== EXPORT ===")

    where = ""
    params: tuple = ()
    if month:
        ym = f"{month[:4]}-{month[4:6]}"
        where  = "WHERE SUBSTR(j.tx_date,1,7)=?"
        params = (ym,)

    journals = conn.execute(
        f"""SELECT j.*, t.source
            FROM journals j
            JOIN transactions t ON j.transaction_id = t.id
            {where}
            ORDER BY j.tx_date, j.transaction_id, j.seq""",
        params,
    ).fetchall()

    if not journals:
        print("  出力する仕訳がありません")
        return

    groups: dict[tuple, list] = {}
    for j in journals:
        ym  = j["tx_date"][:7].replace("-", "")
        key = (j["source"], ym)
        groups.setdefault(key, []).append(j)

    for (source, ym), jlist in sorted(groups.items()):
        counters: dict[str, int] = {}
        voucher_map: dict[int, str] = {}
        rows = []
        for j in jlist:
            tx_id = j["transaction_id"]
            if tx_id not in voucher_map:
                date_key = j["tx_date"].replace("-", "")[2:]
                counters[date_key] = counters.get(date_key, 0) + 1
                code = ACCOUNT_CODE.get(source, "09")
                voucher_map[tx_id] = f"{code}{date_key}{counters[date_key]:02d}"
            rows.append(_journal_to_csv_row(j, voucher_map[tx_id]))

        out_dir  = os.path.join(BASE, _SOURCE_FOLDER[source], "出力ファイル")
        prefix   = _SOURCE_PREFIX[source]
        out_path = os.path.join(out_dir, f"{prefix}_{ym}_zenryoku.csv")
        utf8_path = _write_zenryoku_csv(out_path, rows)
        print(f"  [{source}] {out_path}  ({len(rows)} 行)")
        print(f"          {utf8_path}")

    print()


_SOURCE_FOLDER = {
    "epos":   "クレジットカード",
    "paypay": "銀行",
    "sbi":    "証券",
}

_SOURCE_PREFIX = {
    "epos":   "credit",
    "paypay": "paypay",
    "sbi":    "sbi-syoken",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STATUS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def cmd_status(conn: sqlite3.Connection):
    print("=== STATUS ===")
    print(f"  DB: {DB_PATH}\n")

    print("  [取り込み済みファイル]")
    for r in conn.execute("SELECT source, filename, imported_at, tx_count FROM import_log ORDER BY imported_at"):
        print(f"    {r['source']:10} {r['filename']}  ({r['tx_count']} 件)  {r['imported_at'][:19]}")

    print("\n  [取引件数 (ソース別)]")
    for r in conn.execute(
        "SELECT source, COUNT(*) AS n, SUM(skip) AS skipped FROM transactions GROUP BY source ORDER BY source"
    ):
        print(f"    {r['source']:10} {r['n']:5} 件  (うちスキップ {r['skipped']} 件)")

    print("\n  [仕訳件数 (ソース別)]")
    for r in conn.execute(
        """SELECT t.source, COUNT(*) AS n, SUM(j.is_uncertain) AS unc
           FROM journals j JOIN transactions t ON j.transaction_id=t.id
           GROUP BY t.source ORDER BY t.source"""
    ):
        print(f"    {r['source']:10} {r['n']:5} 行  (うち要確認 {r['unc']} 行)")

    print("\n  [月別仕訳件数]")
    for r in conn.execute(
        """SELECT SUBSTR(j.tx_date,1,7) AS ym, t.source, COUNT(*) AS n
           FROM journals j JOIN transactions t ON j.transaction_id=t.id
           GROUP BY ym, t.source ORDER BY ym, t.source"""
    ):
        print(f"    {r['ym']}  {r['source']:10} {r['n']:4} 行")

    unclassified = conn.execute(
        """SELECT COUNT(*) FROM transactions
           WHERE skip=0 AND id NOT IN (SELECT DISTINCT transaction_id FROM journals)"""
    ).fetchone()[0]
    if unclassified:
        print(f"\n  ⚠ 未分類取引: {unclassified} 件")
    print()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    ap = argparse.ArgumentParser(description="全力会計 SQLite中間形式変換スクリプト")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser("import", help="CSVをDBに取り込む")
    p_import.add_argument("--force", action="store_true", help="重複を無視して再取り込み")
    p_import.add_argument("--include-processed", action="store_true",
                          help="処理済み/ フォルダも対象に含める")

    p_export = sub.add_parser("export", help="journalsから全力会計CSVを出力")
    p_export.add_argument("--month", metavar="YYYYMM", help="特定月のみ出力")

    sub.add_parser("status", help="DB統計を表示")

    args = ap.parse_args()
    conn = init_db()

    if args.cmd == "import":
        cmd_import(conn, force=args.force,
                   include_processed=getattr(args, "include_processed", False))
    elif args.cmd == "export":
        cmd_export(conn, month=getattr(args, "month", None))
    elif args.cmd == "status":
        cmd_status(conn)

    conn.close()


if __name__ == "__main__":
    main()
