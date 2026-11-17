# vauban — 設計メモ

Claude Pro の範囲内で複数の AI とセキュリティツールを組み合わせ、バグ・脆弱性・シークレット漏洩をできるだけ早い段階で検出するマルチレイヤー・コードレビュー構成の設計メモ。

名前は、ルイ14世に仕えた軍事技師セバスティアン・ル・プレストル・ド・ヴォーバンにちなむ。星形要塞と稜堡システムで「縦深防御（defense in depth）」を体系化した人物で、力押しではなく手順と多重の防壁で段階的に守る思想を体現する。本構成も pre-commit → pre-push → CI → PR と防壁を重ね、最奥の主郭に人間の最終確認（**Zion check**）を置く。

実装は `setup.sh` として完成している。

---

## アーキテクチャ概要

コミットの流れに沿って4層でチェックする。

| レイヤー | ツール | タイミング | コスト |
| :--- | :--- | :--- | :--- |
| 実装支援 | Claude Code | ローカル開発中 | Claude Pro |
| pre-commit | detect-secrets | コミット前（ローカル） | 無料 |
| pre-push | Gemini 3.1 Flash Lite | プッシュ前（ローカル・**リポごとのオプトイン**） | 無料 |
| CI | Semgrep / Dependabot | GitHub Actions | 無料 |
| PR レビュー | Qodo Merge | プルリクエスト時 | 無料（個人プラン・制限あり） |

**リポにコミットする層と、手元だけの層を分ける（v1.8.0〜）。**

| 区分 | 層 | 置き場所 | 理由 |
| :--- | :--- | :--- | :--- |
| リポにコミット | detect-secrets / Semgrep / Dependabot | 各リポの設定ファイル | 誰がそのリポを使っても役に立つ。外部からの PR も検査できる |
| 手元だけ | Gemini レビュー | `.git/hooks/pre-push`（コミットされない）＋ vauban 本体のスクリプト | 自分の API キーと判断で動くもの。public リポをフォークした人の差分が、本人の知らないうちに Google へ送られるのを防ぐ |
| vauban の管理外 | Qodo Merge | GitHub App の設定 | 対象リポの選択は GitHub 側で行う。setup.sh は案内を出すだけ |

どの層を入れるかはリポごとに選べる（後述の「セットアップ手順」）。

---

## レイヤー別設計

### Layer 1 — pre-commit: シークレットスキャン

**ツール:** detect-secrets

コミット前にステージされたファイルをスキャンし、APIキー・パスワード等の混入を防ぐ。

**`.pre-commit-config.yaml`（各リポジトリに設置）:**

```yaml
# vauban managed (v1.8.1) — このファイルは vauban の setup.sh が生成します。再実行で上書きされます。
# https://github.com/<owner>/vauban
repos:
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets
        args: ['--baseline', '.secrets.baseline']
        exclude: '(^|/)(node_modules|\.venv|venv|vendor|dist|build|__pycache__)/'
```

このファイルは detect-secrets だけを持つ。v1.7.0 までは Gemini レビューもここ（`repo: local` の pre-push フック）に入れていたが、コミットされるファイルに入れると、リポを clone・フォークした人が `pre-commit install` した時点で、その人の `GEMINI_API_KEY` で差分が送られてしまう。v1.8.0 で Layer 2 は手元だけのフックに移した。

このファイルの `vauban managed` マーカーは「このリポに vauban が入っている」目印も兼ねる（`sync.sh` が導入先を探すのに使う）。

`.secrets.baseline` はリポジトリにコミットする。既知の偽陽性はここに登録して除外する。

**⚠️ baseline は導入直後に必ず棚卸しする:**

`detect-secrets scan > .secrets.baseline` は「**生成時点で見つかった秘密をすべて『既知』として記録**」し、以後のスキャンから除外する。つまり導入前にすでにコミット済みの**本物の鍵があっても自動でホワイトリスト化**され、二度と警告されない。秘密漏洩を防ぐツールが既存の漏洩を見逃す入口になりうる。

そのため、baseline を生成したら**必ず一度 audit を通し**、各エントリが偽陽性か本物かを人が仕分けする:

```bash
detect-secrets audit .secrets.baseline
# 各検出を y（本物）/ n（偽陽性）で仕分け。
# 本物が見つかったら、その秘密をローテーション（無効化＋再発行）し、
# 履歴からの除去も検討する（git filter-repo 等）。baseline で蓋をしない。
```

setup.sh は baseline 新規生成時にこの棚卸しを促すメッセージを表示する。

---

### Layer 2 — pre-push: Gemini によるコードベース整合性チェック

**ツール:** Gemini Flash Lite（既定 `gemini-3.1-flash-lite`・Google AI Studio 無料枠）

差分を Gemini に渡し、コードベース全体との設計上の矛盾・セキュリティ上の懸念を確認する。Semgrep が既知パターンを検出するのに対して、Gemini は文脈・設計レベルの問題を補う。

実体は [`scripts/gemini_review.py`](scripts/gemini_review.py)。ドリフト防止のためここにはコード全文を載せず、振る舞いだけ記す。

**置き方（v1.8.0〜）:**

- スクリプトは各リポに**コピーしない**。setup.sh が各リポの `.git/hooks/pre-push` に短いフックを書き、そこから vauban 本体のスクリプトを直接呼ぶ。vauban を `git pull` すれば全リポに即反映される。
- 依存（`google-genai`）は PC に 1 つの専用 venv（既定 `~/.cache/vauban/venv`。`VAUBAN_VENV` で変更可）に入れ、全リポで共有する。
- **既定は無効（オプトイン）。** リポごとに `git config vauban.gemini true` で有効化する（`setup.sh <repo> --gemini` が設定する）。この設定は `.git/config` に入り、コミットされない。設定を忘れたリポや、設定していない 2 台目の PC では送信されない。送ってはいけないリポで「止め忘れる」より「入れ忘れる」ほうが安全なので、この向きにしている。
- フックは vauban 本体や venv が見つからなければ 1 行出して push を通す。レビューが失敗しても push は止めない。
- 既存の pre-push フック（vauban 管理外）がある場合は上書きせず、警告して Gemini フックを入れない。

**振る舞い:**

1. `get_diff()` で push 対象の差分を取得する。`@{push}` → `@{upstream}` → `origin/HEAD` の順に merge-base 起点で diff を取り、複数コミットをまとめて push しても全体をレビューできるようにする（いずれも失敗したら最後の手段として `HEAD~1..HEAD`）。
2. `GEMINI_API_KEY`（必須）、`GEMINI_MODEL`（任意・既定 `gemini-3.1-flash-lite`）、`GEMINI_TIMEOUT_MS`（任意・既定 10000）を env から読む。
3. 新 SDK（`from google import genai` → `genai.Client(...)`）で差分をレビューさせ、結果を標準出力に表示する。
4. **常に exit 0**（情報表示のみ・push はブロックしない）。

**制約・前提:**

- 使用パッケージは **`google-genai`（新 SDK）**。旧 `google-generativeai` ではない（import 形が異なる）。setup.sh が専用 venv に入れるため、グローバル python への手動インストールは不要。
- `GEMINI_API_KEY` 未設定・SDK 未インストール・API エラー（レート超過等）・**タイムアウト超過**は、いずれもメッセージを出して**スキップ通過**する。CI では動かない（手元だけの層）。
- **pre-push なので必ずタイムアウトを設ける**（`GEMINI_TIMEOUT_MS`・既定 10 秒）。API が応答しないときに `git push` がハングするのを防ぐ。超過時はスキップして push を通す。開発テンポを優先し短め（5 秒では正常応答を切る懸念があり 10 秒に設定）。
- **lock ファイル・自動生成物・ビルド成果物は差分から除外**してからレビューする（`package-lock.json` / `*.lock` / `*.min.js` / `*.map` 等）。これらが混ざると 100k 制限で実コードが切り捨てられ、ノイズと AI 送信面積も増える。除外後に実コード差分が無ければ API を叩かずスキップする。
- **指摘があるときだけ枠付きで強調表示**し、無いときは stderr に 1 行（`問題なし`）。push のログに埋もれて見落とす／オオカミ少年化するのを防ぐ。
- **無料枠で使えるモデルは変わる。** 既定は `gemini-3.1-flash-lite`（2026-07-02 に `gemini-2.5-flash` から変更。`2.5-flash` / `3.5-flash` の無料枠は 1 日 20 回・1 分 5 回しかなく、`3.1-flash-lite` は 1 日 500 回・1 分 15 回）。2026-11-17 時点では `gemini-3.5-flash-lite` も同じ 15 RPM / 250K TPM / 500 RPD だが、短い質問での応答が 2〜5 秒と `3.1-flash-lite`（1〜2 秒）より遅かったため、10 秒で打ち切る pre-push では `3.1-flash-lite` を既定のままにしている。旧 `gemini-2.0-flash` は無料枠が `limit: 0`（=実質不可）に外れたため使わない。無料枠は変動するので、動かなくなったら `GEMINI_MODEL` で差し替える（`python3 -c "import os; from google import genai; [print(m.name) for m in genai.Client(api_key=os.environ['GEMINI_API_KEY']).models.list()]"` で一覧確認）。
- 差分が 100,000 文字を超える場合は先頭のみレビューし、その旨を stderr に表示する（残りは未確認）。
- 結果は表示のみで push はブロックしない。ブロックしたい場合はスクリプト末尾を `exit 1` に変更する。

---

### Layer 3 — CI: Semgrep + Dependabot

#### Semgrep（SAST）

**再利用ワークフロー方式（v1.8.0〜）:** スキャンの中身は vauban 側の [`.github/workflows/semgrep.yml`](.github/workflows/semgrep.yml)（`on: workflow_call`）に 1 つだけ置き、各リポには呼び出すだけの短いファイルを置く。

**各リポの `.github/workflows/semgrep.yml`（setup.sh が生成）:**

```yaml
name: Semgrep
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
  security-events: write   # SARIF を Security タブへ上げるのに必要
jobs:
  semgrep:
    uses: <owner>/vauban/.github/workflows/semgrep.yml@v1
```

- vauban 側を直してタグ `v1` を動かせば、全リポのスキャンが一斉に更新される。配布し直す必要も、全リポに書き込めるトークンも要らない。
- public リポの再利用ワークフローは private リポからも呼べる。
- 代わりに、vauban が全リポの CI の依存先になる。**タグを壊すと全リポの CI が同時に落ちる**ので、変更は main で確認してからタグを動かす。
- 呼び出し側で `permissions` を明示し、`secrets: inherit` は付けない（vauban 側にリポの secrets を渡さない）。
- タグ参照は Semgrep の mutable-action-tag ルールに引っかかるため `nosemgrep` を付けている。SHA pin すると vauban 側の更新が届かなくなるので、意図してタグで呼ぶ。

**vauban 側の再利用ワークフローの中身:**

```yaml
jobs:
  semgrep:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      security-events: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install 'semgrep~=1.166'   # v1系のパッチ/ルール追従を許容しつつ再現性を確保
      - name: Semgrep scan
        run: semgrep scan --config=auto --sarif --output=semgrep.sarif --error
      - name: Upload SARIF to GitHub Security
        if: always()
        continue-on-error: true
        uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: semgrep.sarif
      - name: Upload SARIF artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: semgrep-sarif
          path: semgrep.sarif
```

`--config=auto` は言語を自動判別して公開ルールセットを適用する。Community 版のルールセットのみで pro ルールは含まれない。

**ブロック方針と SARIF:**

- `--error` で**検出があれば job を落とし push/PR をブロック**する（「気づければいい」ではなく「止める」方針）。
- 結果は SARIF で出力し、`if: always()` で**ブロック時も必ず後段に渡す**。
- **Security タブへの掲載は public リポジトリ（または GitHub Advanced Security 契約済み）でのみ可能。** private で未契約だと upload は失敗するが、`continue-on-error: true` のため job は落とさない（ブロックは scan 側が担保）。
- private でも確認できるよう **SARIF を成果物（artifact）としても保存**する。Actions の実行結果からダウンロードできる。
- コンテナ（`semgrep/semgrep` image）はやめて `pip install semgrep` に変更。理由は、`upload-sarif` アクション（Node 製）がコンテナ内だと Node 不在で動かないことがあるため。

**誤検知の抑制（`.semgrepignore`）:**

スキャン対象から外したいパスは `.semgrepignore` に記述する（setup.sh が雛形を生成）。個別の検出だけ黙らせたい場合は、該当行の直前に `# nosemgrep` コメントを置く。「止まるが、正規の手順で黙らせられる」状態を保ち、`--error` の場当たり的な無効化を防ぐ。

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

vauban の `setup.sh` を対象リポジトリに対して実行する。

```bash
# Usage: bash setup.sh <target-repo-path> [ecosystem] [--gemini|--no-gemini] [--semgrep|--no-semgrep] [--remove]

bash ~/vauban/setup.sh ~/my-web-app  npm               # detect-secrets + Semgrep + Dependabot(npm)
bash ~/vauban/setup.sh ~/my-api      pip --gemini      # 上に加えて、この PC で Gemini レビューを有効化
bash ~/vauban/setup.sh ~/my-docs     none --no-semgrep # detect-secrets だけ
bash ~/vauban/setup.sh ~/my-web-app                    # 再実行（今の層のまま最新に更新）
bash ~/vauban/setup.sh ~/my-web-app  --remove          # vauban を外す
```

**層の選び方の目安:**

| リポの種類 | 層 |
| :--- | :--- |
| public（他人がフォーク・DL する） | detect-secrets + Semgrep + Dependabot。Gemini は手元で有効にしてよい（コードは公開済み） |
| private でコードあり | detect-secrets + Semgrep + Dependabot。Gemini は送ってよいリポだけ `--gemini` |
| ドキュメント中心 | detect-secrets だけ（`none --no-semgrep`） |

**層の状態はファイルの有無で表す。** 設定ファイルは置かない。

| ファイル（`vauban managed` マーカー付き） | 意味 |
| :--- | :--- |
| `.pre-commit-config.yaml` | vauban 導入済み（detect-secrets。常に入る） |
| `.github/workflows/semgrep.yml` | Semgrep あり |
| `.github/dependabot.yml` | Dependabot あり（ecosystem もここから読む） |
| `git config vauban.gemini`（コミットされない） | この PC でこのリポの Gemini レビューが有効 |

再実行時に引数を省くと、今あるファイルから層を読み取って引き継ぐ。層を外すと、vauban が生成したファイルを削除する。

### 導入先の管理と一括更新（`sync.sh`）

導入先リポの一覧は持たない。手元の clone のうち `.pre-commit-config.yaml` に `vauban managed` マーカーがあるものを、その都度探す。

```bash
bash ~/vauban/sync.sh              # $HOME の下 3 階層を探し、setup.sh を再実行して差分を表示
bash ~/vauban/sync.sh --commit     # vauban のファイルだけをコミット（既定ブランチにいるときのみ）
bash ~/vauban/sync.sh --push       # コミットして push
bash ~/vauban/sync.sh ~/work       # 探す場所を指定
```

- リポを増やす: そのリポで `setup.sh` を 1 回実行してコミットする。減らす: `setup.sh --remove`。リネームしても目印はリポの中にあるので壊れない。
- 手元で動かし、自分の git / gh の認証で push する。GitHub に全リポへ書き込めるトークンを置かない。
- ローカルの `.git/hooks` も同時に更新される（CI からの配布ではできなかった）。
- vauban のファイル以外の変更はコミットしない。既定ブランチ以外にいるリポはコミットせずスキップする。
- 手元に clone の無い導入先は対象にならない。新しい PC では、使うリポを clone して `setup.sh` を 1 回実行する（ローカルフックの導入に必要）。

### setup.sh 実行後の手動作業

```bash
# 1. 作成されたファイルをコミット
git add .pre-commit-config.yaml .secrets.baseline \
        .github/workflows/semgrep.yml .semgrepignore .github/dependabot.yml
git commit -m "add vauban security setup"
git push

# 2. Gemini レビューを有効にしたなら、API キーを環境変数に設定（~/.zshrc 等）
export GEMINI_API_KEY="your_api_key"

# 3. 必要なら GitHub Marketplace から Qodo Merge を連携（vauban の管理外）
```

### 更新とバージョン（ドリフト対策）

更新の届き方は層によって違う。

| 層 | 本体を直したときの届き方 |
| :--- | :--- |
| Semgrep | vauban のタグ `v1` を動かせば全リポに即反映（各リポのファイルは呼び出しだけ） |
| Gemini レビュー | vauban を `git pull` すれば全リポに即反映（各リポにコピーしていない） |
| detect-secrets・Dependabot・呼び出し用ファイル | 各リポへの**コピー**なので、`sync.sh` で setup.sh を再実行して揃える |

どの repo がどの版かは、生成ファイルのマーカー行に埋め込んだバージョンで分かる。

```bash
bash ~/vauban/setup.sh --version                 # 本体の版
head -1 .pre-commit-config.yaml                  # 各 repo に入っている版（vauban managed (v1.8.0) …）
```

既存の `.secrets.baseline` / `.semgrepignore` は再実行で上書きされない。

**既存ファイルの保護（マーカー方式）:** setup.sh が生成するファイル（`.pre-commit-config.yaml` / `semgrep.yml` / `dependabot.yml` / ローカルの `pre-push` フック）の先頭には `# vauban managed` マーカーを埋め込む。再実行時、対象ファイルが既に存在し**マーカーを含まない場合**（＝ユーザーが自前で用意した設定）は、黙って消さず **`*.bak` に退避してから**上書きする。マーカーを含む（＝前回 vauban が生成した）場合はそのまま上書き＝更新。これにより「更新の上書き」と「既存資産の破壊防止」を両立する。導入先で独自の pre-commit フックや Actions を使っていた場合は `*.bak` から手動でマージする。

```
ローカル実装（Claude Code）
    ↓
git commit
    → detect-secrets がシークレット検出（問題があればコミット失敗）
    ↓
git push
    → Gemini pre-push フックが差分レビュー（有効にしたリポのみ。結果を表示して通過）
    → GitHub Actions で Semgrep が自動実行
    → Dependabot が依存関係を週次でスキャン
    ↓
PR 作成
    → Qodo Merge が自動コメント
    ↓
Zion check（人間の最終確認・最後の砦）→ マージ
```

---

## コスト整理

| ツール | 無料範囲 |
| :--- | :--- |
| Claude Code | Claude Pro に含まれる |
| Gemini（既定 3.1 Flash Lite） | AI Studio の無料枠（レートリミット・モデル別quotaあり。`GEMINI_MODEL` で差替可） |
| detect-secrets | 完全無料（OSS） |
| Semgrep Community | 完全無料（プライベートリポジトリ含む）。ただし SARIF の Security タブ掲載は public / GHAS のみ |
| Dependabot | GitHub 標準機能（無料） |
| Qodo Merge | 個人プラン無料（制限あり・要確認） |

---

## データフロー（外部送信）

セキュリティ構成である以上、**自分のコードがどこへ送られるか**を把握しておく。本構成では、private なソースコードが以下の外部サービスへ送信される。承知の上で使う前提。

| 層 | サービス | 送信される内容 | 送信先 | 備考 |
| :--- | :--- | :--- | :--- | :--- |
| 実装支援 | Claude Code | 会話・ファイル内容 | Anthropic | Claude Pro |
| pre-commit / detect-secrets | （なし） | — | **ローカル完結** | 外部送信なし |
| pre-push / Gemini | Google AI Studio (Gemini API) | **git の差分**（diff） | Google | ⚠️ **無料枠は入力（コード差分）が Google の学習・人間レビューに利用されうる**（有料の Vertex AI とは規約が異なる）。lock/生成物は除外済みだが、実コードは送られる |
| CI / Semgrep | Semgrep（公開ルール取得） | コードは原則ローカルでスキャン。`--config=auto` は**ルールセットを取得**する | semgrep.dev | ログイン無しなら所見のアップロードはしない。SARIF は GitHub 内に留まる |
| CI / Dependabot | GitHub | 依存関係のメタデータ | GitHub | リポジトリと同じ管理下 |
| PR / Qodo Merge | Qodo | **PR の差分・コード** | Qodo (third-party) | GitHub App として権限付与。機微なコードを扱う repo では導入可否を個別判断 |

**判断材料:**

- ⚠️ **Gemini 無料枠の学習利用リスクは重い。** 商用化・特許を狙うコアロジック、契約上社外秘のコード、あるいは誤ってステージした認証情報などが含まれると、規約上 Google 側に学習・閲覧されうる。v1.8.0 から Gemini 層は**既定で無効**で、送ってよいリポだけ `setup.sh <repo> --gemini`（＝ `git config vauban.gemini true`）で有効にする。`GEMINI_API_KEY` をシェル全体に設定していても、有効化していないリポでは送られない。どうしても AI レビューが要るなら、学習に使われない有料経路（Vertex AI 等）への切り替えを検討する。
- 同様に Qodo Merge も第三者にコードが渡る。機微な repo では App を入れない。public リポでは、外部の人が出した PR の内容も Qodo に渡る。
- ローカル完結なのは detect-secrets のみ。「最低限ここだけは効かせる」というミニマム構成も可能。

---

## 注意・限界

**ツールの限界:**

- detect-secrets は新しいシークレットパターンを自動学習しない。ルールは定期的に更新する必要がある。
- Semgrep は既知の脆弱性パターンの検出が主で、アプリ固有のビジネスロジックのバグは検出できない。
- Gemini はコンテキストウィンドウが大きいが、実際に「理解」して矛盾を検出できるかは差分の性質による。false positive・false negative の両方が起きる。
- Dependabot のバージョンアップが安全かどうかの判断は人間が行う。

**人間確認の形骸化リスク（Zion check）:**

最終の人間確認を **Zion check** と呼ぶ。サッカー日本代表GK鈴木彩艶（SUZUKI Zion）にちなむ。GKはすべての防衛ラインをすり抜けてきたシュートを最後に止める役割で、多層防御の「最終ライン」と重なる。「Zion」という名前は聖書では城砦の丘・要塞都市、『マトリックス』では人類最後の砦を指し、ヴォーバンという要塞名のプロジェクトの最奥に立つ守り手として意味が重なる。

すべての防壁を通り抜けたものを最後に止めるこの層が形骸化すると多層構成全体が意味を失う。複数の AI ツールが「問題なし」と判断した後の人間確認は、注意力が下がりやすい。このシステムはバグや脆弱性を発見する機会を増やすものであり、人間がコードを読む習慣の代替ではない。AI の指摘がない状態でのマージは「問題がないこと」を意味しない。Zion check だけは自動化・省略しない。

**公開リポジトリとしての運用チェックリスト（vauban 自身の public/private 境界）:**

vauban 自体が public リポジトリであることに起因する固有の注意点。ファイルを追加・変更するときは以下を確認する。

- 🟢 **公開してよいもの**: 設計思想・アーキテクチャ・各層の設定テンプレート（yaml等）そのもの、コスト表、外部送信データフロー表、変更履歴、外部レビュー履歴、スクリプトの汎用ロジック。this repo の存在意義そのものなので基本は公開前提。
- 🔴 **公開してはいけないもの**: `GEMINI_API_KEY` 等のシークレットの**実値**（プレースホルダのみ可）。コミット前に `detect-secrets` のローカルスキャンに加え、`git log -p` で過去履歴も汚染していないか確認する。
- 🟡 **判断が必要なもの＝実際の private リポジトリ名・個人プロジェクトの実名**: ソースにも**Actions の実行ログにも**出さない。public リポの Actions ログはログインした誰でも読め、Actions の「変数」はログで伏せ字にならない（v1.7.0 まで配布先一覧を変数で渡しており、ログに private リポ名が出ていた）。リポ名を扱う処理は vauban の Actions で走らせず、手元の `sync.sh` で行う。README/DESIGN.md の使用例も実名ではなく汎用名（`my-web-app` 等）を使う。
- ワークフロー（`.github/workflows/*.yml`）を新規追加・変更する際は `permissions:` を必要最小限に明記する。他リポに書き込めるトークンを vauban の secrets に置かない。
- `.claude/settings.local.json` のようなローカル専用設定ファイルは意図的に git 管理に含めない（誤って `git add` しない）。
- 新しいスクリプトを追加するときは、ログ・エラーメッセージ・コメントに個人が特定できる情報（メールアドレス・内部リポジトリ構成・非公開サービスの内部URL等）を書き込んでいないか確認する。

---

## 変更履歴

バージョンは `setup.sh --version` / `python3 scripts/gemini_review.py --version` で確認できる。

### v1.8.1（2026-11-17）— Semgrep に `actions: read` を足す
- private リポで SARIF を上げる手順（`upload-sarif`）が実行情報を読めず、`Resource not accessible by integration` の警告を出していた。検査とブロックには影響しないが、毎回出るので呼び出し側と再利用ワークフローの両方に `actions: read` を足した。
- 再利用ワークフローが求める権限は呼び出し側の許可を超えられない（超えると起動時に失敗する）。そのため先に全リポの呼び出し側を `sync.sh` で更新し、その後でタグ `v1` を動かす。

### v1.8.0（2026-11-17）— 層をリポごとに選べるようにし、配布の仕組みを手元に移す
- **Gemini レビューをリポから外し、手元だけのフックにした。** 各リポの `.pre-commit-config.yaml` と `scripts/gemini_review.py` に入れていたため、public リポを clone・フォークした人が `pre-commit install` すると、その人の `GEMINI_API_KEY` で差分が送られうる状態だった。`.git/hooks/pre-push` から vauban 本体のスクリプトを呼ぶ形にし、**既定は無効**、リポごとに `--gemini`（`git config vauban.gemini true`）で有効化する。再実行すると旧版の `scripts/gemini_review.py` と pre-commit の pre-push フックは片付けられる。
- **Semgrep を再利用ワークフローにした。** 中身は vauban の `.github/workflows/semgrep.yml` に置き、各リポは `uses: <owner>/vauban/...@v1` で呼ぶだけ。タグを動かせば全リポに反映される。
- **setup.sh に層の選択を追加。** `--gemini` / `--no-gemini`、`--semgrep` / `--no-semgrep`、ecosystem に `none` で Dependabot を外す、`--remove` で vauban を外す。層の状態は生成ファイルの有無で表し、再実行時は引き継ぐ。
- **CI からの配布（`distribute.yml`）と自動修正（`rescue.yml` / `scripts/rescue.py`）を廃止し、手元の `sync.sh` に置き換えた。** 理由:
  - 配布先一覧（Actions 変数 `VAUBAN_REPOS`）がログで伏せ字にならず、public の実行ログに private リポ名が出ていた（過去の実行記録はすべて削除済み）。
  - 一覧を手で管理していたためリネーム等でずれていた。
  - 全リポに書き込めるトークン（`GH_PAT`）を public リポの secrets に置く必要があり、その失効にも 2 か月気づかなかった（rescue はエラーを握りつぶして成功扱いになっていた）。
  - CI からはローカルの `.git/hooks` を更新できなかった。
  - `sync.sh` は手元の clone からマーカーで導入先を探し、自分の認証で push する。`GH_PAT` と `VAUBAN_REPOS` は削除した。
- 生成ファイルのマーカー行にバージョンを埋め込むようにした（`# vauban managed (v1.8.0) — …`）。

### v1.7.0（2026-11-17）— フックを動かす Python を 3.10 以上に固定
- `setup.sh` が素の `python3` を使っていたため、macOS で CommandLineTools の Python 3.9（サポート終了）に pre-commit が入り、フックの `INSTALL_PYTHON` も 3.9 に焼き込まれていた。その結果 Gemini レビューの隔離環境が 3.9 で作られ、google-auth が `FutureWarning: ... Python version 3.9 past its end of life` を出していた。
- 3.10 以上の Python を探して（3.13 → 3.12 → 3.14 → 3.11 → 3.10 → `python3` の順）pip 導入・baseline 生成・`pre-commit install` のすべてに使うよう変更。見つからなければ案内を出して終了する。
- 既存の導入先は、手元で `setup.sh` を再実行するとフックが新しい Python に向き直り、隔離環境も初回の push で作り直される（CI の配布ではローカルの `.git/hooks` は更新されない）。

### v1.5.0（2026-07-02）— 公開リポジトリ化に伴うセキュリティ棚卸し
- 🟡 **対象リポジトリ名をソースから除去**: `scripts/rescue.py` / `.github/workflows/distribute.yml` にハードコードされていた個人運用の private リポジトリ名一覧を削除し、GitHub Actions のリポジトリ変数 `VAUBAN_REPOS`（JSON配列）から読む方式に変更。public リポジトリから非公開プロジェクトの存在・名称が読める状態を解消。
- `distribute.yml` / `rescue.yml` に `permissions: contents: read` を明記（両者とも push/PR は自前の `GH_PAT` で行い `GITHUB_TOKEN` は未使用のため最小化）。
- `.gitignore` を追加。
- README/DESIGN.md の使用例を汎用リポジトリ名に置き換え。

### v1.4.0（2026-06-16）— 第三者レビュー（Gemini 3.5 Flash）反映
- 🔴 **既存ファイルの破壊的上書きを防止（マーカー方式）**: 生成ファイルに `# vauban managed` を埋め、再実行時マーカー無しの既存ファイルは `*.bak` に退避してから上書き。導入先の既存 pre-commit/Actions 設定の消失を防ぐ。
- Gemini レビューの差分から **lock/自動生成/ビルド成果物を除外**（100k 制限による実コード切り捨て・ノイズ・送信面積を抑制）。実コード差分が無ければ API を叩かずスキップ。
- **指摘があるときだけ枠付きで強調表示**、無いときは 1 行。見落とし／オオカミ少年化を防止。
- 無料枠 Gemini の **学習利用リスクの警告を格上げ**（秘匿 repo は `GEMINI_API_KEY` 未設定で無効化）。
- タイムアウト既定を **20 秒 → 10 秒**に短縮（開発テンポ優先）。
- Semgrep ピン留めを `==1.166.0` → `~=1.166`（v1 系のルール追従を許容）。
- detect-secrets フックに `exclude` 追加（#7）／依存導入を導入済みならスキップ（#8）。

### v1.3.0（2026-06-16）
- Semgrep を `semgrep==1.166.0` にバージョンピン留め（再現性・サプライチェーン対策）。
- MIT `LICENSE` を追加（公開準備）。

### v1.2.0（2026-06-16）— 堅牢性
- Gemini API に HTTP タイムアウト（`GEMINI_TIMEOUT_MS`・既定 20 秒）を追加。pre-push で API が応答しないと `git push` がハングする問題を解消。
- `setup.sh` の依存導入を PEP 668（externally-managed）対応で 3 段フォールバック化。失敗時は中途半端に止まらず pipx 案内を出して終了。
- Gemini フックを `language: python` + `additional_dependencies: [google-genai]` に変更し、依存を pre-commit の隔離環境で管理（グローバル python 依存による無言スキップを根絶）。

### v1.1.0（2026-06-16）— 改名＋Layer 2 修正＋運用整備
- **改名**: pj-cerberus → **vauban**。最終人間レビュー層を **Zion check** と命名。
- **Layer 2（Gemini）の致命的不具合を修正**: 旧 SDK `google-generativeai` → 新 `google-genai`／pre-commit フックに `verbose: true`（exit 0 でも結果表示）／`get_diff` を `@{push}→@{upstream}→origin/HEAD` の merge-base 起点に／モデルを `GEMINI_MODEL` 化・既定 `gemini-2.5-flash`（`gemini-2.0-flash` は無料枠 `limit:0` で離脱）／100k 超の差分切り捨てを警告化。
- **Semgrep**: SARIF 出力＋Security タブ連携（`if: always` / `continue-on-error`）＋ artifact 保存。`--error` のブロックは維持。`.semgrepignore` 雛形を生成。
- **運用**: `.secrets.baseline` の棚卸し手順（`detect-secrets audit`）を明記／外部送信のデータフロー表を追加／`VAUBAN_VERSION`・`--version` でコピー配布のドリフトを可視化。

---

## 外部レビュー履歴

設計・コードの死角を減らすため、開発に関与していない別ベンダーの LLM に二次レビューを依頼している（ベンダーを散らして指摘の重複・共倒れを避ける方針）。

### 2026-06-16 — Google AI Studio / Gemini 3.5 Flash

- **依頼方法:** `vauban-review-bundle.md`（対象ファイル全文＋対応済み論点＋焦点プロンプト）を貼り、優先度別（🔴/🟡/🟢）で批判的にレビューさせた。
- **主な指摘と対応（→ v1.4.0 で反映）:**
  - 🔴 **既存 `.pre-commit-config.yaml` / workflow の無条件上書き** → マーカー方式（`# vauban managed`）で既存資産を `*.bak` 退避してから上書き。
  - 🟡 lock/生成ファイルで実コードが 100k 制限で切り捨て → `get_diff` で除外。
  - 🟡 exit 0 の見落とし・オオカミ少年化 → 指摘時のみ強調表示／コード差分ゼロなら API 不発。
  - 🟡 無料枠 Gemini の学習利用リスク → 警告を格上げ。
  - 🟡 timeout 20s が長い → 10s。/ 🟡 semgrep ピン留めのドリフト → `~=1.166` に緩和。
  - 🟢 detect-secrets フックの `exclude`／導入済みなら install skip。
- **議論の上で見送り:** `semgrep-action` への全面移行（コンテナ/Node 依存が戻るため）、SCA 即時実行（npm/pip audit）、全 repo 一括同期ラッパー。いずれも「個人向けの軽量・引き締まったスコープ」を維持するための判断。
- 詳細な差分は [変更履歴 v1.4.0](#変更履歴) を参照。
