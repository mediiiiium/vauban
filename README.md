# pj-cerberus (Project Cerberus)

Claude Pro の範囲内で複数の AI とセキュリティツールを組み合わせ、バグ・脆弱性・シークレット漏洩をできるだけ早い段階で検出するマルチレイヤー・コードレビュー構成のメモ。

## アーキテクチャ概要

コミットの流れに沿って4層でチェックする。

| レイヤー | ツール | タイミング | コスト |
| :--- | :--- | :--- | :--- |
| 実装支援 | Claude Code | ローカル開発中 | Claude Pro |
| pre-commit | detect-secrets / truffleHog | コミット前（ローカル） | 無料 |
| pre-push | Gemini 2.0 Flash + Semgrep | プッシュ前（ローカル） | 無料 |
| CI | Semgrep / Dependabot | GitHub Actions | 無料 |
| PRレビュー | Qodo Merge | プルリクエスト時 | 無料（個人プラン） |

## 各ツールの役割

### Claude Code（実装フェーズ）
ローカルで機能実装・リファクタリングを行う。`/code-review` や `/security-review` スラッシュコマンドでセルフレビューも可能。

### detect-secrets / truffleHog（シークレットスキャン）
APIキーやパスワードのコミット混入を防ぐ pre-commit フック。

```bash
pip install detect-secrets
detect-secrets scan > .secrets.baseline
detect-secrets audit .secrets.baseline
```

pre-commit フックとして設定すると、コミット時に自動チェックされる。

### Gemini 2.0 Flash（コードベース全体との整合性チェック）
100万トークンのコンテキストウィンドウを活かし、変更がコードベース全体の設計・既存ロジックと矛盾していないかを確認する。

Claude Code から Gemini API を呼び出し、変更差分と関連ファイルを渡してレビューさせる（pre-push フック or 手動実行）。

```bash
export GEMINI_API_KEY="your_api_key"  # Google AI Studio で無料発行
```

### Semgrep（SAST：静的解析）
OWASP Top 10 を含む既知の脆弱性パターンを静的解析で検出する。Community 版は無料でプライベートリポジトリにも使用可能。

```bash
pip install semgrep
semgrep --config=auto .
```

GitHub Actions に組み込むと PR ごとに自動実行できる（`.github/workflows/semgrep.yml`）。

### Dependabot（依存関係スキャン）
使用ライブラリの既知の脆弱性（CVE）を自動検出し、バージョンアップの PR を自動作成する。GitHub 標準機能で、パブリック・プライベート問わず無料。

`.github/dependabot.yml` を作成するだけで有効になる。

### Qodo Merge（PRレビュー）
プルリクエストに対して AI がロジック・セキュリティ・テストカバレッジの観点でコメントする。CodeRabbit は公開リポジトリ向けの無料プランが中心だが、Qodo は個人プランでプライベートリポジトリにも対応している。

[Qodo Merge](https://www.qodo.ai/products/merge/) を GitHub App として連携するだけで動作する。

## ワークフロー

```
ローカル実装（Claude Code）
    ↓
git commit → detect-secrets / truffleHog がシークレット検出
    ↓
git push 前 → Semgrep（ローカル）+ Gemini でレビュー → 問題あれば修正
    ↓
git push → GitHub Actions で Semgrep / Dependabot が自動実行
    ↓
PR 作成 → Qodo Merge が自動コメント
    ↓
人間が最終確認 → マージ
```

## コスト整理

| ツール | 無料範囲 |
| :--- | :--- |
| Claude Code | Claude Pro に含まれる |
| Gemini 2.0 Flash | AI Studio の無料枠（レートリミットあり） |
| detect-secrets / truffleHog | 完全無料（OSS） |
| Semgrep Community | 完全無料（プライベートリポジトリ含む） |
| Dependabot | GitHub 標準機能（無料） |
| Qodo Merge | 個人プラン無料（プライベートリポジトリ対応） |

## セットアップ手順

### 1. シークレットスキャン（detect-secrets）

```bash
pip install detect-secrets pre-commit
# .pre-commit-config.yaml に設定を追加
pre-commit install
```

### 2. Gemini API キー

```bash
export GEMINI_API_KEY="your_api_key"
```

### 3. Semgrep（GitHub Actions）

`.github/workflows/semgrep.yml` を作成：

```yaml
name: Semgrep
on: [push, pull_request]
jobs:
  semgrep:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: returntocorp/semgrep-action@v1
        with:
          config: auto
```

### 4. Dependabot

`.github/dependabot.yml` を作成（言語に合わせて調整）：

```yaml
version: 2
updates:
  - package-ecosystem: "npm"  # or pip, cargo, etc.
    directory: "/"
    schedule:
      interval: "weekly"
```

### 5. Qodo Merge

GitHub Marketplace から Qodo Merge を連携するだけで PR 時に自動起動する。

## 注意・限界

- Gemini 呼び出しの具体的なスクリプト（pre-push フック）は別途実装が必要。
- Semgrep は既知パターンの検出が中心で、アプリ固有のビジネスロジックのバグは検出できない。
- Dependabot は脆弱性の通知のみで、修正の判断は人間が行う。
- AI レビュー（Gemini / Qodo）の指摘は正確性の保証がないため、マージ前の人間確認は省略できない。
