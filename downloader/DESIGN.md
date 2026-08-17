# Downloader 設計書

## 1. 目的

`dl_model_list.yaml` を読み込み、大容量ファイルをストリーミングで取得する。
ダウンロード対象は `--category` で明示的に指定されたトップレベルキーだけとする。
カテゴリ未指定時は全カテゴリを対象にせず、何もダウンロードしない。
ファイルは YAML の `disabled` により無効化できる。

実装上の必須条件は次のとおり。

- HTTP 通信は `asyncio` と `httpx.AsyncClient` を使用する。
- スレッドを生成・利用しない。
- パーツデータを別の一時ファイルへ保存せず、出力ファイルの指定位置へ直接書く。
- 同時接続数を制限し、HTTP通信のバッファ管理は `httpx.AsyncClient` に任せる。
- `split` と `range` の複数URLを並列に処理する。
- 完了後に出力ファイル全体の SHA256 を計算して検証する。

## 2. 重要な設計判断

### 2.1 `aiofiles` は使用しない

`aiofiles` は非同期APIに見えるが、通常のファイル操作を executor のスレッドで実行する。
そのため、スレッドを使用しないという要件に反する。

ファイルI/Oは標準の `open`, `seek`, `write`, `truncate`, `os.unlink` を使用する。
HTTPの各チャンクを小さく受信し、書き込み区間だけ同期I/Oを行う。大容量ファイル全体をメモリへ載せない。

これはディスクI/O中にイベントループが一時的に停止するトレードオフを伴うが、スレッド不使用を優先する。

### 2.2 出力ファイルへ直接書き込む

パーツごとの一時ファイルは作成しない。出力先を最終パスとして作成し、`truncate(total_size)` で必要サイズを確保した後、各タスクが `seek(offset)` して書き込む。

出力パスは次の形式とする。

```text
MODELS_ROOT/<dir>/<category>/<file>
```

`MODELS_ROOT` は `MODEL_DL_ROOT/models` とする。

不完全ファイルを完成品と誤認しないため、次のロックファイルを使用する。

```text
<output>
<output>.download.json
```

正常終了後にロックファイルを削除する。プロセス異常終了時はロックファイルが残るため、再実行時に不完全ファイルを検証してから再利用するか、最初から作り直す。

`.part` のような別名の大容量ファイルは作らない。したがって、一時ファイル容量はメタデータファイルとログを除き、受信中のメモリバッファだけである。

### 2.3 asyncioの並列単位

ファイル単位の executor は作らない。各ファイルの各パーツを `asyncio.create_task` で起動し、グローバルな `asyncio.Semaphore` で同時HTTPストリーム数を制限する。

ファイルへの `seek` と `write` は、出力ファイルごとの `asyncio.Lock` の中で実行する。
`seek` と対応する `write` は必ず同じロック区間に置く。

## 3. YAMLスキーマ

```yaml
category-name:
  - type: range
    disabled: false
    dir: checkpoints
    file: model.safetensors
    sha256: optional-64-hex-digits
    filesize: 9,433,061,528
    urls:
      - https://example.invalid/mirror-a/model
      - https://example.invalid/mirror-b/model

  - type: split
    disabled: false
    dir: diffusion_models
    file: model.safetensors
    sha256: optional-64-hex-digits
    urls:
      - https://example.invalid/model.part01
      - https://example.invalid/model.part02
    part-sizes:
      - 4,294,967,296
      - 3,707,873,976
```

### 3.1 数値の解釈

`filesize` と `part-sizes` は YAML の整数または文字列を受け付ける。
文字列からは `,` と `_` を除去して10進整数として解釈する。
`1GiB` 等の単位対応を追加する場合は、同じパーサーをCLIにも使用する。
負数、0、浮動小数、桁区切りの位置が不正な値はエラーとする。

例:

```text
9433061528
9,433,061,528
9_433_061_528
```

### 3.2 フィールドの優先順位と齟齬の扱い

`range` の総サイズ:

1. HF/CivitAI APIから取得したサイズ
2. 対象URLの `HEAD` の `Content-Length`
3. YAML の `filesize`
4. 取得不能ならエラー

`split` のパーツサイズ:

1. 各URLの `HEAD` の `Content-Length`
2. YAML の `part-sizes`
3. 取得不能ならエラー

YAMLの `filesize` と `part-sizes` は参考値であり、サーバーから取得した値を優先する。
サーバー値とYAML値が異なる場合はwarningを出してサーバー値で続行する。
サーバー値が複数URL間で異なる場合は、同一ファイルを構成できないためエラーとする。
実際の受信バイト数は、解決したサーバー値またはYAML fallback値と必ず一致させる。

## 4. rangeの仕様

`range` は同一ファイルを複数URLからRange取得する方式である。

1. `filesize` またはメタデータ解決で総サイズを確定する。
2. CLIまたは設定値のチャンクサイズで `[start, end]` の範囲を作る。
3. チャンクをURLへラウンドロビンで割り当てる。
4. 各リクエストに `Range: bytes=start-end` を付ける。
5. HTTPステータスが `206` であることを確認する。
6. `Content-Range` の開始位置、終了位置、総サイズが要求と一致することを確認する。
7. 出力ファイルの `start` へストリーミング書き込みする。

`200 OK` で全体データを返すサーバーはRange非対応として失敗させる。全体を返すレスポンスを切り詰めて使ってはならない。

最後のチャンクは `filesize` に合わせて短くする。リトライ時は毎回 `seek(start)` から書き直す。

## 5. splitの仕様

`split` はURL順に連結するパーツである。

1. `part-sizes` またはHEADで全パーツサイズを確定する。
2. 累積サイズから各パーツの出力オフセットを算出する。
3. 全体サイズで出力ファイルを事前確保する。
4. 各URLを並列ストリーミング取得する。
5. パーツの受信開始時に対象オフセットへ移動し、受信バイト数が指定サイズと一致することを確認する。

パーツのレスポンスが指定サイズを超えた場合はエラーにする。少ない場合もエラーにする。
Google Drive等でHEADが使えないURLは `part-sizes` を必須とする。

## 6. メタデータAPI

既存実装の意味を再利用するが、同期APIクライアントは使用しない。

### 6.1 Hugging Face

- HF URLから `repo_id` とファイルパスを解析する。
- `HF_TOKEN` が設定されていれば、APIおよびダウンロードリクエストに `Authorization: Bearer <token>` を付ける。
- `httpx.AsyncClient` でHF APIまたはresolveレスポンスからサイズを取得する。
- LFSメタデータにSHA256があれば、YAMLのSHA256が空の場合の検証値として使用する。

既存の `HfApi` を直接呼び出してはならない。同期処理のため、厳密なスレッド不使用要件と両立しない。

### 6.2 CivitAI

- URLの `models/<version-id>` とクエリの `fileId` を解析する。
- `CIVITAI_API_TOKEN` が設定されていればAPI認証に使用する。
- `/api/v1/model-versions/<version-id>` の結果から、指定された `fileId` と一致するファイルを選択する。
- APIのサイズとSHA256を利用する。

version IDだけでファイルを選択してはならない。複数ファイルを持つバージョンでは、URLの `fileId` を優先する。

### 6.3 ハッシュの優先順位

1. YAMLの `sha256`
2. HF/CivitAI APIから得たSHA256
3. 取得不能なら検証をスキップし、警告ログを出す

検証をスキップした場合もダウンロード成功にはできるが、結果ログに `unverified` を明示する。

### 6.4 メタデータ解決フェーズ

ダウンロード前に全対象のメタデータを取得するフェーズ1を実行する。

- `range`: サイズとSHA256を解決する
- `split`: 各パーツサイズを解決し、合計サイズを計算する
- YAML値はサーバー情報が取得できない場合のfallbackとして使用する
- YAML値とサーバー値の差異はwarningとして記録する
- メタデータ取得不能、URL間のサーバー値不一致はエラーとする
- フェーズ1で1件でも失敗した場合、通常実行はフェーズ2へ進まず全体を中止する
- `--dry-run` でもフェーズ1を実行し、1件でも失敗した場合は全体を中止して終了コード1とする

## 7. ハッシュ検証

各パーツのハッシュを連結して全体ハッシュとみなしてはならない。
全パーツの書き込み完了後、出力ファイルを先頭から一定サイズずつ読み、全体のSHA256を計算する。

SHA256不一致の場合は成功扱いにせず、cleanなエラー終了時にはロックファイルを削除する。
プロセス異常終了時に残ったロックファイルは、次回起動時にstale判定する。

## 8. 接続数制御

アプリケーション独自の総バッファ上限は設けない。
HTTP通信のバッファ管理は `httpx.AsyncClient` に任せ、同時接続数だけを明示的に制限する。

- `asyncio.Semaphore` で同時ダウンロードタスク数を制限する
- `httpx.Limits(max_connections=...)` でも接続プールを制限する
- `--max-concurrent` は同時接続数の上限として扱う
- `--buffer-limit` は廃止する
- `IO_CHUNK_SIZE` はSHA256読み込みなどの内部処理に使ってよいが、総バッファ量の計算には使わない

## 9. 再実行と同時実行

- 完成ファイルがあり、期待SHA256がある場合は検証一致でスキップする。
- SHA256が空の場合、既存ファイルをサイズ一致だけで安全と判断しない。原則として警告して再検証または再ダウンロードする。
- `.download.json` が残るファイルは不完全とみなし、同じファイルのダウンロードを再開せず、対象範囲を上書きする。
- 同じ出力ファイルを別プロセスが処理しないよう、ロックファイルを排他的に作成する。

## 10. CLI

最低限、次を提供する。

```text
--category NAME[,NAME...]
--list
--dry-run
--chunk-size SIZE
--max-concurrent N
--no-progress
--verbose
```

`--category` はダウンロード対象カテゴリのホワイトリストである。

- `--category sd15` は `sd15` だけを対象とする
- `--category sd15,flux2` は指定された2カテゴリだけを対象とする
- `--category` 未指定時は何もダウンロードしない
- YAMLの `disabled: true` は常にスキップする
- 未知のカテゴリ指定は設定ミスとして終了コード2にする

## 11. 既存環境変数

- `MODEL_DL_ROOT`: YAMLの `dir` の基準ディレクトリ
- `MODEL_DL_LIST`: YAMLファイルパス
- `MODEL_DL_LOG`: ログファイルパス
- `HF_TOKEN`: 設定時のみHF API/ダウンロードに付与
- `CIVITAI_API_TOKEN`: 設定時のみCivitAI API/ダウンロードに付与
- `CIVITAI_API_URL`: CivitAI APIの基底URL

## 12. Google Drive

### 12.1 対応方針

Google Driveの大容量公開ファイルでは、通常のGETが「ウイルススキャンできません」確認ページのHTMLを返すことがある。
`gdown` はこの確認ページを回避できるが、同期APIであり、内部で `.part` 一時ファイルを使用するため、本Downloaderへ直接組み込まない。

Downloaderでは、gdownの実装を参考にしたasyncio対応のGoogle Drive URL resolverを使用する。

- URLからファイルIDを抽出する
- `drive.google.com/uc?id=<id>` にCookieを保持してアクセスする
- `download-form`、hidden input、確認URL、`downloadUrl`を解析する
- 確認URLへ再アクセスし、最終的なファイルURLを得る
- 解決後のレスポンスを既存のストリーミング書き込みへ渡す

### 12.2 Google Driveの適用範囲

Google Drive URLは `type: split` で使用する。
Google DriveのRange対応はサーバー仕様に依存するため、`type: range` では原則使用しない。
`part-sizes` はGoogle Drive URLでは必須とする。HEADでサイズを取得できないことがあるためである。

Google Driveの確認ページや最終URLに含まれるCookie・確認パラメータは、同一の `httpx.AsyncClient` 内で保持する。
認証情報や確認用トークンをログへ出してはならない。
Google Driveのsplitエントリでは、SHA256をYAMLで指定することを基本運用とする。
SHA256がない場合はwarningを強化して `unverified` と明示するが、形式エラーにはしない。

### 12.3 Google Driveの再試行

大容量ファイルでは接続が途中で終了する可能性がある。
リトライ時は、可能なら既に受信した位置から残りのRangeを要求し、`Content-Range`を検証して出力位置へ追記する。
Range再開に対応しない場合は、パーツ先頭から再取得する。

## 13. エラーと終了コード

- YAML形式不正、必須項目不足、サイズ不正: 終了コード2
- HTTP、書き込み、Range検証、サイズ検証、SHA256検証失敗: 終了コード1
- `--dry-run` はメタデータ解決と既存ファイル状態確認まで行い、ダウンロードしない。メタデータ失敗時は全体を中止して終了コード1
- 一部ファイルだけ失敗した場合も、全体の終了コードは1
