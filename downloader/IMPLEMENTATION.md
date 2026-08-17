# Downloader 実装指示書

## 1. 実装対象

`downloader/download.py` を新設計で書き直す。
必要に応じて `run_download.sh` の依存関係とデフォルトのYAMLパスも更新する。
既存の `dl_list_sample.yaml` の旧スキーマ互換は実装しない。

## 2. 絶対条件

- `threading`, `concurrent.futures`, `asyncio.to_thread`, `loop.run_in_executor` を使用しない。
- `aiofiles` を使用しない。依存追加もしない。
- `httpx.Client` と同期HTTP APIを使用しない。HTTPは `httpx.AsyncClient` に統一する。
- パーツ単位の一時ファイルを作らない。
- 全体ファイルを `bytes` や `bytearray` に読み込まない。
- `seek` と `write` は同一ファイル用の `asyncio.Lock` の中で実行する。
- 全体SHA256は、全書き込み完了後に出力ファイルを読み直して計算する。
- アプリケーション独自の総バッファ上限は設けない。バッファ管理はhttpxに任せる。

## 3. 推奨ファイル構成

まずは保守性を優先し、次の2ファイル以内に収める。

```text
downloader/download.py
downloader/run_download.sh
```

HTTPプロバイダ処理が長くなる場合のみ、`provider_metadata.py` へ分離する。
不要な小規模モジュールを増やさない。

## 4. 実装順序

### Step 1: 定数、型、サイズパーサー

次を実装する。

```python
def parse_size(value: object) -> int
```

要件:

- `int` を受け付ける。
- 文字列の前後空白を除去する。
- `,` と `_` を除去する。
- 10進整数を受け付ける。
- 必要なら `KiB`, `MiB`, `GiB`, `KB`, `MB`, `GB` を受け付ける。
- 0以下、不正な文字列、指数表記、浮動小数は拒否する。

`filesize` と `part-sizes` の各値に必ず適用する。

### Step 2: YAML検証とカテゴリ選択

トップレベルがmappingであることを確認する。
各カテゴリの値はリスト、各エントリはmappingであることを確認する。

エントリごとに次を検証する。

- `type` は `range` または `split`
- `file` は空でない相対パス
- `dir` は相対パスで、`..` による脱出を許可しない
- `urls` は1件以上のHTTP/HTTPS URL
- `range` の `filesize` は存在時に正の整数
- `split` の `part-sizes` は存在時にURL数と同じ要素数
- `part-sizes` の合計は正の整数
- `sha256` は空または64桁16進数

出力パスは `realpath(MODEL_DL_ROOT / models / dir / category / file)` を計算し、`MODEL_DL_ROOT` 配下であることを確認する。

`disabled: true` のエントリは、メタデータ取得もダウンロードも行わない。

カテゴリ選択はホワイトリスト方式とする。

- `--category` 指定時は指定カテゴリだけを対象にする
- `--category` 未指定時は対象カテゴリを空集合として、何もダウンロードしない
- 未知カテゴリは終了コード2とする

### Step 3: AsyncClientの構成

`main()` で1個の `httpx.AsyncClient` を生成し、全タスクで共有する。

- `follow_redirects=True`
- connect/read/write/pool timeoutを明示する
- `limits` で接続数を制限する
- URLごとに必要なAuthorizationヘッダを組み立てる

APIと実データ取得で同じクライアントを使用する。タスクごとにClientを生成しない。

### Step 4: メタデータ解決

ダウンロード前にフェーズ1として全対象のメタデータを解決する。
解決結果はエントリへ正規化して保存し、後続の既存ファイル判定とダウンロードで同じ値を使う。
1件でも解決に失敗した場合は、通常実行・`--dry-run`ともフェーズ2へ進まず全体を中止する。
`--dry-run` の終了コードは1とする。

#### range

URLがHFまたはCivitAIの場合はAPI情報を取得する。取得できなければ各URLのHEADを試す。
YAMLの `filesize` はサーバー情報が取得できない場合のfallbackとして使用する。

API/HEADで取得したサイズがYAMLの `filesize` と異なる場合はwarningを出し、サーバー値で続行する。
API/HEADで取得したサイズが複数URL間で異なる場合は、同一ファイルを構成できないためエラーにする。
SHA256も同様に、サーバーAPIの値を優先し、YAML値との差異はwarningとする。

#### split

各URLへHEADを行い、各レスポンスの `Content-Length` を優先する。
HEADで取得できない場合だけYAMLの `part-sizes` をfallbackとして使用する。
HEADの値がYAMLの `part-sizes` と異なる場合はwarningを出し、HEAD値で続行する。
取得不能なパーツにYAML値もない場合はエラーにする。

HEADの値は信頼しきらず、GET時の実受信バイト数を必ず検証する。
フェーズ1で解決したサイズと、実際の受信バイト数が一致しない場合は失敗させる。

#### HF API

- URLの `/resolve/<revision>/` より前をrepo ID、後ろをファイルパスとして解析する。
- `HF_TOKEN` がある場合だけBearerヘッダを追加する。
- 既存の `hf_get_path_info` のサイズ・LFS SHA256取得意図を、非同期HTTPで再実装する。
- 同期の `HfApi` は呼ばない。

#### CivitAI API

- URLからversion IDと`fileId`クエリを解析する。
- `/model-versions/<version-id>` を非同期GETする。
- `fileId` がある場合は応答中のファイルIDと完全一致する項目を選ぶ。
- `fileId` がない場合だけ既存実装相当の既定選択を行う。
- サイズの単位をAPI仕様に従ってbytesへ変換する。

#### ハッシュ優先順位

YAMLの `sha256`、プロバイダAPIのSHA256、未検証の順とする。
未検証の場合はwarningを出し、結果を `unverified` と記録する。

### Step 5: 出力ファイルの準備

対象ファイルについて次を行う。

1. `MODEL_DL_ROOT/models/<dir>/<category>` の親ディレクトリを作成する。
2. `<output>.download.json` を排他的に作成する。既存の場合は、同一プロセスの所有物でない限りエラーにする。
3. 既存の出力ファイルがあれば、期待ハッシュがある場合だけ全体検証してスキップ判定する。
4. ダウンロード対象なら出力ファイルを `w+b` で開く。失敗時に旧ファイルを保持する方式ではないため、開始前にこの挙動をログへ出す。
5. `truncate(total_size)` する。

同一プロセス内では、出力パスごとに `asyncio.Lock` を1個用意する。
異なるプロセスとの競合はロックファイルの排他的作成で防ぐ。

### Step 6: 共通パーツ取得関数

次のような関数を実装する。

```python
async def download_segment(
    client, url, output_file, output_lock,
    offset, expected_size, headers, semaphore, retries
) -> int
```

処理:

1. `async with semaphore` に入る。
2. 試行ごとにHTTP GETを開始する。
3. Range指定時はステータス `206` を要求する。
4. `Content-Range` を解析し、開始、終了、総サイズを検証する。
5. split時は必要に応じて `Content-Length` を検証する。
6. 応答を `aiter_bytes(chunk_size=io_chunk_size)` で読む。
7. 受信した各チャンクについて、`asyncio.Lock` 内で `seek(offset + received)` と `write(chunk)` を行う。
8. 受信総量が期待値を超えたら失敗する。
9. 応答終了時に受信総量が期待値と一致しなければ失敗する。
10. 失敗した試行の部分書き込みは、次の試行で同じ範囲を先頭から上書きする。
11. 指数バックオフしてリトライする。

受信処理をファイルロックの外で行い、書き込みだけをロック内に置くこと。

`Accept-Encoding: identity` をsplit/rangeのGETとHEADに指定し、圧縮転送によるサイズ解釈の差を避ける。

同時実行数は `asyncio.Semaphore` と `httpx.Limits(max_connections=...)` で制限する。
`--buffer-limit` は使用しない。`httpx.AsyncClient` のストリーミングと接続プールにバッファ管理を委ねる。

#### Google Drive resolver

gdownをDownloaderから直接呼び出してはならない。gdownの同期APIはイベントループをブロックし、内部の `.part` 一時ファイルは本設計の制約に反する。

gdownの確認ページ回避ロジックを参考に、`httpx.AsyncClient` 用のresolverを実装する。

```python
async def resolve_google_drive_url(
    client: httpx.AsyncClient, url: str
) -> str
```

処理:

1. `drive.google.com`、`drive.usercontent.google.com`、`docs.google.com` のURLを検出する
2. URLからGoogle DriveのファイルIDを抽出する
3. Cookieを保持したまま `drive.google.com/uc?id=<id>` へアクセスする
4. `Content-Disposition` があれば直接ファイルURLとして採用する
5. HTMLの場合は `download-form`、hidden input、`downloadUrl`、確認用hrefを解析する
6. hidden inputとCookieを保持して確認URLへ再アクセスする
7. HTMLが再び返る場合は、最大回数を設定して解決を繰り返す
8. 確認ページ、権限エラー、quota超過を区別して報告する

HTML解析は標準ライブラリの `html.parser.HTMLParser` を優先し、gdownやBeautifulSoupを実行時依存にしない。
解決用のHTML全体を無制限にメモリへ読み込まず、上限を設ける。

Google Drive URLは `split` のみ正式対応とする。`range` でGoogle Drive URLが指定された場合は、Range対応を保証できないため事前にエラーにする。
`part-sizes` がないGoogle Driveのsplitエントリは、HEAD不能に備えてエラーにする。

大容量Google Driveでは接続が途中終了する可能性があるため、リトライ時は可能なら既受信位置から残りのRangeを要求する。
Range再開に対応しない場合はパーツ先頭から再取得する。

### Step 7: split実装

パーツサイズからoffsetを計算し、各URLについて `download_segment` をタスク化する。
`asyncio.TaskGroup` を使用し、いずれかが失敗したら残りをキャンセルする。

全タスク終了後、受信量が各 `part-sizes` と一致していることを確認する。

### Step 8: range実装

`filesize` をchunk sizeで分割する。
最後の範囲を `filesize - 1` で切る。
URLはラウンドロビンで割り当てる。

各タスクに次のヘッダを渡す。

```text
Range: bytes=<start>-<end>
Accept-Encoding: identity
```

HTTPサーバーが `200` を返した場合は失敗させる。
`Content-Range` がない場合も失敗させる。

### Step 9: SHA256と完了処理

全パーツ終了後、標準の `open(path, "rb")` で一定サイズずつ読み、SHA256を計算する。
計算は同期処理だが、ファイル全体をメモリに載せない。

- 期待値あり、一致: 成功
- 期待値あり、不一致: エラー。完成扱いにしない
- 期待値なし: warningを出し `unverified` とする

成功したらロックファイルを削除してから処理結果を記録する。
出力ファイルのリネームは行わない。出力ファイルは最初から最終パスに書いているためである。

### Step 10: CLIと終了処理

`asyncio.run(main_async())` のみをエントリポイントにする。

最低限の引数:

```text
--category NAME[,NAME...]
--list
--dry-run
--chunk-size SIZE
--max-concurrent N
--no-progress
--verbose
```

`--max-concurrent` は同時ダウンロード接続数の上限として使用する。
httpxの `Limits(max_connections=...)` と同じ上限を設定し、バッファ量の計算には使用しない。

## 5. ログと結果

ファイルごとに次をログへ出す。

- category
- type
- URL数
- 総サイズ
- 実効並列数
- SHA256検証結果 (`verified` / `unverified` / `mismatch`)
- 成功、スキップ、失敗

パスワード、Bearer token、CivitAI tokenをログへ出してはならない。

## 6. テスト指示

実装後、外部サイトに依存しないローカルHTTPテストを追加または実行する。

最低限確認するケース:

1. `parse_size` の整数、カンマ区切り、アンダースコア、不正値
2. splitの2パーツ並列書き込みと全体SHA256
3. rangeの206とContent-Range検証
4. rangeで最後のチャンクだけ短いケース
5. 200応答を返すRange非対応サーバーの失敗
6. 途中切断後のリトライが対象範囲を先頭から上書きすること
7. サイズ超過・サイズ不足の失敗（決定的エラーとしてリトライしないこと）
8. category未指定時に何もダウンロードしないホワイトリスト動作
9. disabledカテゴリ、disabledエントリ、category絞り込み
10. 既存ファイルのSHA256一致スキップ
11. サーバー値とYAML値の差異がwarningで継続されること
12. サーバー値がURL間で異なる場合の失敗
13. SHA256空欄時の `unverified` 警告
14. HF_TOKENがある場合だけAuthorizationが付くこと
15. CivitAI_TOKENがURLではなくAuthorizationヘッダに付くこと
16. CivitAI_TOKENがログやURLへ漏れないこと
17. CivitAIのfileIdで正しいファイルが選択されること
18. Google Drive確認HTMLから最終URLを解決できること
19. Google DriveのCookieとhidden inputを保持できること
20. SHA256なしのGoogle Drive splitで強いwarningと `unverified` が記録されること
21. Google DriveのRange方式を拒否すること
22. Google Drive接続切断後のRange再開または先頭再取得
23. フェーズ1のメタデータ失敗時にフェーズ2へ進まず全体中止すること
24. `threading` や `concurrent.futures` がimportされていないこと

`--list` は実データへアクセスせずにカテゴリと無効項目を表示する。
`--dry-run` は実データ本体をダウンロードしないが、フェーズ1のメタデータ取得（API/HEAD/Google Drive resolver）と既存ファイル状態確認は実行する。
フェーズ1に1件でも失敗した場合は全体を中止し、終了コード1とする。

## 7. run_download.sh

新実装では `huggingface_hub` と `littledl` をインストールしない。
必要な依存は `httpx`, `pyyaml`, `tqdm` とする。
`aiofiles` は厳密なスレッド不使用要件のため追加しない。
`gdown` も直接使用しない。gdownの確認ページ回避ロジックだけをasyncio実装の参考にする。

YAMLのデフォルトパスは、実際に使用する設定に合わせて `dl_model_list.yaml` とする。
実行するPythonスクリプトのパスは `run_download.sh` 自身のディレクトリを基準にする。
