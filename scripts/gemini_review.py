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
    # ブロックしたい場合は "問題なし" を含まないときに exit 1 に変更する
    sys.exit(0)
