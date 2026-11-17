#!/usr/bin/env python3
# vauban の Gemini レビュー本体。各リポにはコピーせず、setup.sh が入れるローカルの
# pre-push フック（.git/hooks/pre-push）から、この場所のまま直接呼ばれる。
import sys
import os
import json
import subprocess
from datetime import datetime, timezone

# setup.sh の VAUBAN_VERSION と揃える。vauban を git pull すれば全リポに即反映される。
__version__ = "1.8.1"

# lock ファイル・自動生成物・ビルド成果物は差分から除外する。
# これらが混ざると 100k 文字制限で実コードが切り捨てられ、レビューから漏れる。
# また AI へ送るノイズ・プライバシー面積も減らせる。
EXCLUDES = [
    "--",
    ".",
    ":(exclude)*package-lock.json",
    ":(exclude)*yarn.lock",
    ":(exclude)*pnpm-lock.yaml",
    ":(exclude)*poetry.lock",
    ":(exclude)*Pipfile.lock",
    ":(exclude)*composer.lock",
    ":(exclude)*Cargo.lock",
    ":(exclude)*go.sum",
    ":(exclude)*.lock",
    ":(exclude)*.min.js",
    ":(exclude)*.min.css",
    ":(exclude)*.map",
]

# このフックは有効にした全リポジトリで git push のたびに Gemini 無料枠(RPD)を1件消費する。
# 同じ API キーの無料枠を他のツールと共有していると、記録が無いまま枠が切れ、
# 枠切れ時に有料 API へ切り替わる別のツールの課金原因を追えなくなる。
# ログを各リポジトリ内に置くと git 管理対象へ紛れ込むため、リポジトリ外の1ファイルに
# 集約する（1 行 1 リクエストの JSON Lines。ts / source / model / tokens）。
USAGE_LOG = os.environ.get(
    "GEMINI_REVIEW_USAGE_LOG",
    os.path.expanduser("~/.cache/vauban/gemini_usage.jsonl"),
)


def _repo_name() -> str:
    """ログの source 名に使うリポジトリのディレクトリ名。"""
    try:
        top = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if top:
            return os.path.basename(top)
    except (subprocess.CalledProcessError, OSError):
        pass
    return os.path.basename(os.getcwd()) or "unknown"


def record_usage(model: str, tokens: int) -> None:
    """1リクエスト＝1行を追記する。

    目的は「無料枠を削った事実」を残すことなので、API がエラー／タイムアウトで
    返った場合もリクエスト自体は届いている前提で tokens=0 の行を書く
    （RPD の1件として数えたい）。API を叩く前にスキップした場合は呼ばない。
    ログの失敗で push を壊さないよう、例外は握りつぶす。
    """
    try:
        now = datetime.now(timezone.utc)
        entry = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
            "source": f"gemini-review/{_repo_name()}",
            "model": model,
            "tokens": int(tokens or 0),
        }
        os.makedirs(os.path.dirname(USAGE_LOG), exist_ok=True)
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        # ログ記録の失敗でレビュー本体・push を止めない。
        pass


def get_diff() -> str:
    # push 対象（= ローカルにあって push 先にまだ無い変更）を優先的に拾う。
    # merge-base 起点なので、複数コミットをまとめて push しても全体をレビューできる。
    # @{push}     … push 先の追跡ブランチ（pre-push で最も正確）
    # @{upstream} … upstream 追跡ブランチ
    # origin/HEAD … リモートの既定ブランチ（新規ブランチで upstream 未設定のとき）
    candidates = [
        ["git", "diff", "--merge-base", "@{push}", "HEAD", *EXCLUDES],
        ["git", "diff", "--merge-base", "@{upstream}", "HEAD", *EXCLUDES],
        ["git", "diff", "--merge-base", "origin/HEAD", "HEAD", *EXCLUDES],
    ]
    for cmd in candidates:
        try:
            diff = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            if diff.strip():
                return diff
        except subprocess.CalledProcessError:
            continue

    # 最後の手段: 直近コミットのみ
    try:
        return subprocess.check_output(
            ["git", "diff", "HEAD~1", "HEAD", *EXCLUDES],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return ""


def review(diff: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("[Gemini] GEMINI_API_KEY が未設定のためスキップします。", file=sys.stderr)
        return ""

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[Gemini] google-genai 未インストールのためスキップします。", file=sys.stderr)
        return ""

    if len(diff) > 100_000:
        print(
            "[Gemini] 差分が大きいため先頭 100,000 文字のみレビューします（残りは未確認）。",
            file=sys.stderr,
        )
        diff = diff[:100_000] + "\n... (差分が大きいため省略)"

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

    # 無料枠で使えるモデルは変わるため env で差し替え可能にする。
    # 例: gemini-flash-latest など（要・最新の無料枠確認）
    model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")

    # pre-push フックなので、API が応答しないと push がハングする。
    # 開発テンポを優先し既定 10 秒。超過時は例外→スキップして push を通す。
    timeout_ms = int(os.environ.get("GEMINI_TIMEOUT_MS", "10000"))

    try:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
        response = client.models.generate_content(model=model, contents=prompt)
        # usage_metadata はモデル/SDK 版によって欠けることがある。取れなくても
        # リクエスト1件は消費しているので、tokens=0 で行だけは必ず残す。
        try:
            tokens = int(getattr(response.usage_metadata, "total_token_count", 0) or 0)
        except Exception:
            tokens = 0
        record_usage(model, tokens)
        return response.text or ""
    except Exception as e:
        record_usage(model, 0)
        print(f"[Gemini] スキップします（API エラー / タイムアウト）: {e}", file=sys.stderr)
        return ""


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(f"vauban gemini_review {__version__}")
        sys.exit(0)

    diff = get_diff()

    # lock/生成物を除いた実コード差分が無ければ、API を叩かずスキップ（ノイズ削減）。
    if not diff:
        print("[Gemini] レビュー対象のコード差分なし。スキップします。", file=sys.stderr)
        sys.exit(0)

    print("[Gemini] レビュー中...", file=sys.stderr)
    result = review(diff)

    if not result:
        sys.exit(0)

    # 指摘が無いときは静かに 1 行だけ（push ログに埋もれて問題ない）。
    cleaned = result.strip().rstrip("。")
    if cleaned == "問題なし":
        print("[Gemini] 問題なし", file=sys.stderr)
        sys.exit(0)

    # 指摘があるときだけ枠で囲んで目立たせる（端末なら色付き）。
    use_color = sys.stdout.isatty()
    yel = "\033[1;33m" if use_color else ""
    rst = "\033[0m" if use_color else ""
    bar = "═" * 56
    print(f"\n{yel}╔{bar}╗{rst}")
    print(f"{yel}║  ⚠  Gemini レビュー指摘あり — 確認してください{rst}")
    print(f"{yel}╚{bar}╝{rst}")
    print(result)
    print(f"{yel}{'─' * 58}{rst}\n")

    # 常に exit 0（情報表示のみ、プッシュはブロックしない）。
    # ブロックしたい場合は、ここで sys.exit(1) に変更する。
    sys.exit(0)
