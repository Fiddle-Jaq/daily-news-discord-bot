# Daily News Discord Bot

毎朝、日本と世界の重要ニュースをGeminiで選定・要約してDiscordへ投稿するBot(無料運用)。
仕様は SPEC.md。作業中の段階に関係する節だけ読むこと(全文を毎回読まない)。

## 進め方
- 実装は SPEC.md §13 の段階順。1段階ずつ進め、その段階の完了条件(検証コマンド)が通るまで次へ進まない
- 段階の完了時は、実行したコマンドとその出力を示す。「動くはず」で済ませない
- SPEC.md に書かれていない機能・依存・設計変更は、実装前に提案して確認を取る
- 使わないと決めたもの: asyncio、Docker、DB、pydantic-settings、pre-commit、respx

## コマンド
- 依存の同期: `uv sync --locked`
- Lint/整形: `uv run ruff check . && uv run ruff format --check .`
- 型: `uv run mypy`
- テスト: `uv run pytest`
- Geminiなしの確認: `uv run --env-file .env daily-news --dry-run --no-llm`
- `daily-news` コマンドは段階1で作る(それまでは存在しない)

## 秘密情報(最重要)
- `.env` は読まない・編集しない。Webhook URL・APIキーをログ・出力・コミット・Discord投稿に含めない
- 例外をログに出すときは必ず `describe_error()` を通す。`logger.exception` と `exc_info=True` は使わない
- 秘密値は `pydantic.SecretStr` で保持し、`get_secret_value()` は送信直前の1か所だけ
- 詳細は SPEC.md §9.7

## コーディング規約
- Python 3.13、パッケージ管理は uv のみ(`pip install` は使わない)
- `mypy --strict` を通す。feedparser の戻り値は境界で即 pydantic モデルに変換する
- datetime は常に timezone-aware(UTC 保持、表示のみ JST)。naive datetime は使わない
- 外部I/O(HTTP・Gemini・現在時刻・sleep)は引数で注入できる形にし、テストはモックで行う
- 環境変数は空文字を「未設定」として扱い、既定値を使う

## 外部サービスの扱い
- ライブラリ・モデル名・GitHub Actions の仕様は、記憶に頼らず最新の公式ドキュメントで確認する
- GitHub Actions のバージョンは、記憶や SPEC の値を使わず、gh api でタグの実在と、使う入力が action.yml にあることを確認してから書く(メジャーのみのタグがない場合もある)
- 実際のGemini呼び出しは SPEC §13 段階5の確認以外では行わない(無料枠の節約)。普段の検証はモックで行う
- 本番のDiscordチャンネルには投稿しない。開発中の `.env` はテスト用Webhookのみ

## Git・権限
- コミットは段階ごと、Conventional Commits 形式(`feat:` `fix:` `test:` `chore:` `docs:`)
- `git push` はユーザーが行う。`git config` と `.claude/settings*.json` は変更しない
- `gh secret set` は実行しない(ユーザーが手動で行う)

## 応答
- ユーザーへの説明は日本語。コード・識別子・コミットメッセージは英語
