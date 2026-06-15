# pj-cerberus — 設計メモ

Claude Pro の範囲内で複数の AI とセキュリティツールを組み合わせ、バグ・脆弱性・シークレット漏洩をできるだけ早い段階で検出するマルチレイヤー・コードレビュー構成の設計メモ。

実装は `setup.sh` として完成している。

---

## アーキテクチャ概要

コミットの流れに沿って4層でチェックする。

| レイヤー | ツール | タイミング | コスト |
| :--- | :--- | :--- | :--- |
| 実装支援 | Claude Code | ローカル開発中 | Claude Pro |
| pre-commit | detect-secrets | コミット前（ローカル） | 無料 |
| pre-push | Gemini 2.0 Flash | プッシュ前（ローカル） | 無料 |
| CI | Semgrep / Dependabot | GitHub Actions | 無料 |
| PR レビュー | Qodo Merge | プルリクエスト時 | 無料（個人プラン・制限あり） |

---

## レイヤー別設計

### Layer 1 — pre-commit: シークレットスキャン

**ツール:** detect-secrets

コミット前にステージされたファイルをスキャンし、APIキー・パスワード等の混入を防ぐ。

**`.pre-commit-config.yaml`（各リポジトリに設置）:**

```yaml
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.4.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']

  - repo: local
    hooks:
      - id: gemini-review
        name: Gemini Code Review
        entry: python3 scripts/gemini_review.py
        language: system
        stages: [pre-push]
        pass_filenames: false
        always_run: true
```

detect-secrets（pre-commit）と Gemini レビュー（pre-push）を両方ここで管理する。`.git/hooks/` を直接編集しないため、ファイルとしてコミット可能。

`.secrets.baseline` はリポジトリにコミットする。既知の偽陽性はここに登録して除外する。

---

### Layer 2 — pre-push: Gemini によるコードベース整合性チェック

**ツール:** Gemini 2.0 Flash（Google AI Studio 無料枠）

差分を Gemini に渡し、コードベース全体との設計上の矛盾・セキュリティ上の懸念を確認する。Semgrep が既知パターンを検出するのに対して、Gemini は文脈・設計レベルの問題を補う。

**`scripts/gemini_review.py`（各リポジトリに設置）:**

```python
#!/usr/bin/env python3
import sys
import os
import subprocess


def get_diff() -> str:
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()
        diff = subprocess.check_output(
            ["git", "diff", f"origin/{branch}...HEAD"], text=True
        )
        if diff:
            return diff
    except subprocess.CalledProcessError:
        pass

    try:
        return subprocess.check_output(["git", "diff", "HEAD~1..HEAD"], text=True)
    except subprocess.CalledProcessError:
        return ""


def review(diff: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Gemini] GEMINI_API_KEY が未設定のためスキップします。", file=sys.stderr)
        return ""

    try:
        import google.generativeai as genai
    except ImportError:
        print("[Gemini] google-generativeai 未インストールのためスキップします。", file=sys.stderr)
        return ""

    if len(diff) > 100_000:
        diff = diff[:100_000] + "\n... (差分が大きいため省略)"

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = f"""以下のコード差分をレビューしてください。

観点:
- 既存コードとの設計上の矛盾（命名規則・パターンの一貫性）
- 並行処理・非同期処理のバグの可能性
- エラーハンドリングの漏れ
- セキュリティ上の懸念（入力検証・認証・シークレットの扱いなど）

指摘がなければ「問題なし」とだけ返してください。
指摘がある場合はファイル名・行番号・内容を箇条書きで返してください。

---
{diff}
"""

    response = model.generate_content(prompt)
    return response.text


if __name__ == "__main__":
    diff = get_diff()

    if not diff:
        print("[Gemini] 差分なし。スキップします。")
        sys.exit(0)

    print("[Gemini] レビュー中...", file=sys.stderr)
    result = review(diff)

    if not result:
        sys.exit(0)

    print("\n=== Gemini レビュー結果 ===")
    print(result)
    print("===========================\n")

    # 常に exit 0（情報表示のみ、プッシュはブロックしない）
    # ブロックしたい場合: "問題なし" を含まないとき exit 1 に変更する
    sys.exit(0)
```

**制約:**

- `GEMINI_API_KEY` が未設定の場合はスキップして通過する（CI では Gemini なしで動く）
- 差分が 100,000 文字を超える場合は自動的に省略する
- 結果は表示のみでプッシュはブロックしない。ブロックしたい場合はスクリプト末尾を `exit 1` に変更する

---

### Layer 3 — CI: Semgrep + Dependabot

#### Semgrep（SAST）

**`.github/workflows/semgrep.yml`:**

```yaml
name: Semgrep
on:
  push:
    branches: [main]
  pull_request:
jobs:
  semgrep:
    runs-on: ubuntu-latest
    container:
      image: semgrep/semgrep
    steps:
      - uses: actions/checkout@v4
      - run: semgrep scan --config=auto --error
```

`--config=auto` は言語を自動判別して公開ルールセットを適用する。Community 版のルールセットのみで pro ルールは含まれない。

#### Dependabot（依存関係スキャン）

**`.github/dependabot.yml`（npm の例）:**

```yaml
version: 2
updates:
  - package-ecosystem: "npm"
    directory: "/"
    schedule:
      interval: "weekly"
    groups:
      all-dependencies:
        patterns:
          - "*"
```

`groups` を設定しないと依存関係ごとに個別 PR が作成される。`groups` でまとめることで週1本に抑えられる。

GAS（Google Apps Script）・WordPress 等は Dependabot の対象外のため設定不要。

---

### Layer 4 — PR: Qodo Merge

GitHub Marketplace から Qodo Merge を GitHub App として連携すると、PR 作成時にロジック・セキュリティ・テストカバレッジの観点で自動コメントされる。

個人プランは無料でプライベートリポジトリに対応しているが、月あたりの PR 数や機能に制限がある。最新の制限は [Qodo の料金ページ](https://www.qodo.ai/pricing/) で確認すること。

---

## セットアップ手順

pj-cerberus の `setup.sh` を対象リポジトリに対して実行するだけで全ファイルが展開される。

```bash
# Usage: bash setup.sh <target-repo-path> [npm|pip|none]

bash ~/pj-cerberus/setup.sh ~/now-on-tap      npm
bash ~/pj-cerberus/setup.sh ~/podcast         pip
bash ~/pj-cerberus/setup.sh ~/pj-sora         pip
bash ~/pj-cerberus/setup.sh ~/brewdrop        none
bash ~/pj-cerberus/setup.sh ~/delivery_route  none   # delivery-app
bash ~/pj-cerberus/setup.sh ~/delivery_log    none
bash ~/pj-cerberus/setup.sh ~/mediiiiium      none   # mediiiiium-web
```

### setup.sh 実行後の手動作業

```bash
# 1. Gemini API キーを環境変数に設定（~/.zshrc 等に追記推奨）
export GEMINI_API_KEY="your_api_key"

# 2. GitHub Marketplace から Qodo Merge を各リポジトリに連携

# 3. 作成されたファイルをコミット
git add .pre-commit-config.yaml .secrets.baseline \
        scripts/gemini_review.py \
        .github/workflows/semgrep.yml
# Dependabot を設定した場合は追加
git add .github/dependabot.yml
git commit -m "add pj-cerberus security setup"
git push
```

---

## ワークフロー

```
ローカル実装（Claude Code）
    ↓
git commit
    → detect-secrets がシークレット検出（問題があればコミット失敗）
    ↓
git push
    → Gemini pre-push フックが差分レビュー（結果を表示して通過）
    → GitHub Actions で Semgrep が自動実行
    → Dependabot が依存関係を週次でスキャン
    ↓
PR 作成
    → Qodo Merge が自動コメント
    ↓
人間が最終確認 → マージ
```

---

## コスト整理

| ツール | 無料範囲 |
| :--- | :--- |
| Claude Code | Claude Pro に含まれる |
| Gemini 2.0 Flash | AI Studio の無料枠（レートリミットあり） |
| detect-secrets | 完全無料（OSS） |
| Semgrep Community | 完全無料（プライベートリポジトリ含む）|
| Dependabot | GitHub 標準機能（無料） |
| Qodo Merge | 個人プラン無料（制限あり・要確認） |

---

## 注意・限界

**ツールの限界:**

- detect-secrets は新しいシークレットパターンを自動学習しない。ルールは定期的に更新する必要がある。
- Semgrep は既知の脆弱性パターンの検出が主で、アプリ固有のビジネスロジックのバグは検出できない。
- Gemini はコンテキストウィンドウが大きいが、実際に「理解」して矛盾を検出できるかは差分の性質による。false positive・false negative の両方が起きる。
- Dependabot のバージョンアップが安全かどうかの判断は人間が行う。

**人間確認の形骸化リスク:**

複数の AI ツールが「問題なし」と判断した後の人間確認は、注意力が下がりやすい。このシステムはバグや脆弱性を発見する機会を増やすものであり、人間がコードを読む習慣の代替ではない。AI の指摘がない状態でのマージは「問題がないこと」を意味しない。
