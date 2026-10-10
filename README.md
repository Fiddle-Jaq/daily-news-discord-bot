# Daily News Discord Bot

毎朝、日本と世界の重要ニュースを Gemini で選んで要約し、Discord のチャンネルに1通のメッセージで投稿する Bot です。
GitHub Actions・Discord Webhook・Gemini API の無料枠だけで動かすことを想定しています。

- 日本 最大7件・世界 最大4件。政治・経済・社会・国際・科学技術が対象(芸能・スポーツ・ゴシップは除外)
- 各項目は、日本語の見出し・1〜2文の要約・出典名・元記事へのリンク
- 詳しい仕様は [SPEC.md](SPEC.md) を参照

## 処理の流れ

1. GitHub Actions が毎朝 06:07 JST に起動する(`.github/workflows/daily.yml`)
2. `feeds.toml` の RSS を取得する(記事の本文は取得せず、タイトルと概要だけを使う)
3. 時間窓・投稿済みURL・重複で候補を絞る
4. Gemini が候補から選び、同じ出来事の記事をまとめ、日本語で要約する
5. Discord Webhook に投稿する
   - Gemini が使えないとき(無料枠の上限・障害など)は、編集済みフィードの新着からタイトルとリンクを機械的に並べる。その場合は投稿に理由が出る
6. 投稿の記録(`state.json`)を Bot がリポジトリにコミットし、同じ日の二重投稿と、前日の記事の再投稿を防ぐ

## セットアップ

### 1. 必要なもの

- [uv](https://docs.astral.sh/uv/)(Python 3.13 は uv が用意します。`pip install` は使いません)
- GitHub アカウントと、このリポジトリのコピー(fork または自分のリポジトリ)
- 投稿先の Discord チャンネルの Webhook URL
  - チャンネルの編集 → 連携サービス → ウェブフック で作成します
  - **Webhook URL は、知っていれば誰でもそのチャンネルに投稿できる認証情報です**。公開の場所に貼らないでください
- Gemini API キー(Google AI Studio で発行します)

### 2. ローカルで動かす

```bash
uv sync --locked
cp .env.example .env    # DISCORD_WEBHOOK_URL と GEMINI_API_KEY を書く(コミットしない)

uv run --env-file .env daily-news --check-feeds        # 各フィードの疎通と鮮度
uv run --env-file .env daily-news --dry-run --no-llm   # Gemini を使わずに投稿内容を確認(投稿しない)
uv run --env-file .env daily-news --dry-run            # Gemini を1回呼んで投稿内容を確認(投稿しない)
```

- 開発中は、本番とは別の**テスト用チャンネル**の Webhook を使ってください
- 設定できる環境変数は `.env.example` と SPEC.md §4.1 を参照(空文字は未設定として扱い、既定値を使います)
- `--dry-run` を付けずに実行すると実際に投稿し、カレントディレクトリの `state.json` を作成・更新します

### 3. GitHub Actions で毎朝動かす

1. リポジトリの Secrets に2つの値を登録する(値は対話入力で渡し、シェル履歴に残さない)
   ```bash
   gh secret set DISCORD_WEBHOOK_URL
   gh secret set GEMINI_API_KEY
   ```
2. Actions タブで `daily` ワークフローを手動実行し、`dry_run` で動作を確認する
3. 以後は毎朝 06:07 JST に自動で動きます
   - `schedule` は混雑時に遅れることがあります(数時間遅れることもあります)。遅れても、その日の分を投稿します
   - 失敗すると GitHub から失敗メールが届きます(アカウントの通知設定で Actions の通知を有効にしておく)。失敗した日は `workflow_dispatch` で手動で再実行できます

### 定期実行が止まったときの確認方法

チャンネルに投稿が来ない日が続いたら(月に1回の定期確認でも)、次で直近の定期実行を見ます。

```bash
gh run list --workflow daily.yml --event schedule --limit 5
```

- 毎日の実行が並んでいて `success` なら正常です
- 失敗(`failure`)が並んでいたら、実行のログ(`gh run view <ID> --log`)で原因を見ます
- 何日も実行がないときは、ワークフローが無効になっています(60日ルールなど。下の「無料で運用するための条件」を参照)。GitHub の Actions のページで `daily` ワークフローを開き、**Enable workflow** で再有効化します(`gh workflow enable daily.yml` でも同じ)。止まっていた日の分は、必要なら手動実行で1回だけ投稿できます

## 無料で運用するための条件

次の条件は、各サービスの都合で変わることがあります。運用中も定期的に確認してください。

- **GitHub Actions**: 公開リポジトリでは、標準のランナーは無料です。非公開リポジトリでは月ごとの無料枠(GitHub Free で 2,000分)の対象になります。この Bot の1回の実行は1分程度です
- **公開リポジトリの60日ルール**: 公開リポジトリでは、60日間アクティビティがないと `schedule` が自動で無効になります。無効になると失敗メールも届かないため、**チャンネルに投稿が来ないことが最後の合図**になります。止まっていないかは、月に1回、上の「定期実行が止まったときの確認方法」で確認してください(Bot のコミットが活動として数えられるかは未確認のため)
- **Gemini API の無料枠**: 1日1回、リクエスト1回(入力 約15,000トークン)で収まる想定です
  - 2026-10-09 時点の `gemini-3.5-flash` の無料枠は RPM 5 / TPM 250K / RPD 20 です。モデルや上限は変わることがあり、`gemini-3.5-flash` は公式に Legacy と表記されています。`GEMINI_MODEL` で差し替えられます
  - 無料枠では、入力と出力が Google の製品改善に使われることがあります(この Bot は公開ニュースのタイトルと概要だけを送ります)

## RSS の利用について

- `feeds.toml` に並べた各 RSS の利用条件は、**配信元ごとに異なります**。使う前に、各配信元の利用規約・RSS の利用条件を自分で確認してください
- この Bot は、**個人・非営利の利用**(自分や少人数のチャンネルへの配信)を想定しています。商用利用や、不特定多数への再配信は想定していません
- 記事の本文は取得しません。投稿には見出し・短い要約・元記事へのリンクだけを載せます。フィードの追加・削除は `feeds.toml` で行えます

## 注意事項

- **要約は AI(Gemini)によるもので、誤りを含むことがあります**。元記事どうしで事実が食い違う場合に一方を断定するなどの例があります。正確な内容は、必ずリンク先の元記事で確認してください(投稿のフッターにも注記が出ます)
- この Bot と投稿内容は、**各ニュース配信元とは無関係**です。配信元が提供・承認・監修したものではありません
- Webhook URL と API キーは、ログ・投稿・`state.json`・コミットに出さない作りになっています(SPEC.md §9.7)。fork して改造する場合も、この扱いを崩さないでください

## 開発

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

テストは外部サービスに接続しません(HTTP・Gemini・時刻はモックで差し替えます)。

## ライセンス

[MIT License](LICENSE)(Copyright (c) 2026 Fiddle-Jaq)。対象はこのリポジトリのコードです。RSS の記事や、Bot が投稿する内容には及びません(各配信元の利用条件に従ってください)。
