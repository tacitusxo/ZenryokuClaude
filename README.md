# ZenryokuClaude

Claude Code で動かす AI 経理ワークフロー。
銀行・クレジットカードの明細CSV を SQLite に取り込み、Claude と対話しながら仕訳を行い、全力会計インポート形式で書き出すスクリプトと、Claude Code へのプロンプト設定一式です。

## 主な機能

- **マルチ口座対応**: クレジットカード / 銀行 / 証券 / 個人立替 / 旅費
- **SQLite中間DB**: 取り込み済みファイルの重複防止・仕訳履歴管理
- **全力会計エクスポート**: 22列フォーマットの Shift-JIS CSV を自動生成
- **Claude Code スキル**: `仕訳して` / `領収書を整理して` の自然言語ワークフロー

## ディレクトリ構成

```
ZenryokuClaude/
  accounting.py          # メインスクリプト
  accounting.db          # ← .gitignore対象（自動生成）
  CLAUDE.md              # Claude Code への指示
  .claude/
    skills/
      keiri-shiwake/
        skill.md         # 仕訳スキル定義
  クレジットカード/      # クレジットカード明細
  銀行/                  # 銀行口座明細
  証券/                  # 証券口座明細
  個人立替/              # 個人立替経費
  旅費/                  # 旅費・交通費
  勘定科目/              # 全力会計カスタム勘定科目定義
  領収書/                # 領収書PDF（YYYY/MM/で月別管理）
  開始残高/              # 会計ソフト移行時の開始残高
```

各口座フォルダの構成:

```
{口座名}/
  {ダウンロードしたCSV}.csv   ← ここに置く
  出力ファイル/               ← 変換済みCSVが出力される
  処理済み/                   ← 処理後に移動
```

## セットアップ

**必要なもの**: Python 3.11+、Claude Code

```bash
git clone https://github.com/yourname/accounting-oss.git
cd accounting-oss
```

設定ファイルの調整（`accounting.py` 冒頭）:

```python
# 代表者のカタカナ氏名（PayPay明細スキップ判定に使用）
REPRESENTATIVE_KATAKANA = ["代表者セイ", "代表者メイ"]  # ← 自社の代表者名に変更

# 役員報酬との相殺金額
SALARY_OFFSET_AMOUNT = 1_200_000  # ← 実際の金額に変更
```

## 使い方

### 1. CSV の取り込み

各口座フォルダのルートにダウンロードした明細CSVを置く:

```
クレジットカード/明細.csv
銀行/明細.csv
証券/明細.csv
```

### 2. Claude Code で仕訳

```
仕訳して
```

Claude が自動で import → classify → export を実行します。

### 3. 手動コマンド

```bash
python3 accounting.py import         # CSVをDBに取り込む
python3 accounting.py status         # 未分類取引の確認
python3 accounting.py export         # 全力会計CSVを出力
python3 accounting.py import --force # 再取り込み（重複上書き）
python3 accounting.py export --month 202601  # 特定月のみ出力
```

## 対応会計ソフト

現在のエクスポートは **全力会計** の22列インポート形式に対応しています。
他の会計ソフト向けには `ZENRYOKU_HEADER` と `_write_zenryoku_csv()` を変更してください。

## .gitignore について

実データ（DB・明細CSV・領収書PDF）は `.gitignore` で除外されています。
リポジトリにはスクリプトとフォルダ構造のみが含まれます。

## ライセンス

MIT License
