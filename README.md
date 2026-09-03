# Site URL Collector v3

## Actions
Target URL / Collection mode / Output mode / Concurrency / Requests per second / Fresh start の6項目です。

Collection mode:
- AUTO: XML＋HTMLで可能な限り収集
- XML_ONLY: XMLだけ高速収集
- HTML_CRAWL: HTML内部リンクから収集

Output mode:
- URL_ONLY: URL・ページタイトル・発見経路・発見元URL・第1階層・第2階層
- DETAIL: 上記＋Title・H1・Status Code・Canonical等

推奨STEP1: AUTO + URL_ONLY + Fresh start ON
STEP2: 同じTarget URLで DETAIL。保存済みURLに詳細情報を追加します。

ログには XMLから発見 / HTMLから追加 / 重複除外 / 現在のユニークURL をリアルタイム表示します。
DETAIL時は進捗バーも表示します。

速度はActions画面から変更できます。
- Concurrency：同時処理数。初期値6
- Requests per second：サイト全体への最大アクセス数/秒。初期値1.5

通常は `6 / 1.5` 推奨です。robots.txtを尊重し、429/503/504時は自動減速します。

URL_ONLYでもHTML探索中に約100ページごとに進捗バーを表示します。HTML探索では新しいURLが途中で増えるため、進捗率は「現時点の処理済み＋確認待ち」を基準にした動的な目安です。
AUTOは網羅性優先でXML掲載URLもHTML巡回するため、XML_ONLYより時間がかかります。


## v3.2 の変更点

### URL正規化・重複対策
- `http://` は `https://` に統一
- `utm_*` / `gclid` / `fbclid` / `yclid` / `msclkid` などの追跡パラメータを除去
- 正規化したURLをSQLiteの主キーとして登録し、正規化後URLでも重複除外
- URLパスの大文字・小文字は、別ページの可能性があるため勝手に統一しない

### ログ文言
`重複除外 ○○ URL` ではなく、実態に合わせて
`同一URLを再発見 ○○ 回`
と表示します。

### AUTO + URL_ONLY のページタイトル
AUTO/HTML_CRAWLで内部リンク探索のために取得したHTMLから `<title>` を同時に保存します。
タイトル取得のための追加HTTPアクセスは行いません。

URL_ONLYのExcel列:
1. URL
2. ページタイトル
3. 発見経路
4. 発見元URL
5. 第1階層
6. 第2階層

注意: XMLで発見されたもののHTML巡回上限等で実ページをまだ取得していないURLは、ページタイトルが空欄になる場合があります。


## v3.3 の変更点

- 末尾スラッシュの自動統一を撤回しました。
- `/page` と `/page/` はサーバーによって別ページの可能性があるため、収集段階では別URLとして保持します。
- `http → https` の統一、追跡パラメータ除去、fragment除去、正規化後の完全一致URL重複除外は継続します。


## v3.4 の変更点

### Actions の Target URL
- `https://www.insource.co.jp/` のデフォルト入力を削除しました。
- Target URL は毎回空欄から入力します。

### Artifact のダウンロード内容
- 過去の output フォルダ全体は Artifact に含めません。
- 今回の実行で生成された対象サイトの Excel / CSV だけを `run_output/` にコピーしてアップロードします。
- 過去の output や state は GitHub 側に残しても、ダウンロード用 ZIP には混在しません。

### Artifact 名
- `site-url-output-123` のような名前ではなく、
  `<対象ホスト名>_URL調査結果`
  の形式にしました。
- 例: `school.recruit-ms.co.jp_URL調査結果`


## v3.5 の変更点

URL収集のリアルタイム進捗に以下を追加しました。

- 経過時間
- 処理速度（ページ/秒）
- 残り時間の目安
- 完了予想時刻（JST）

AUTO / HTML_CRAWL では巡回中に新しいURLが追加されるため、残り時間と完了予想は推定値です。


## v3.6 の変更点 — 大規模サイトの分割・再開対応

- HTML巡回キューをSQLiteに保存します。
- 各ページ処理後に進捗を保存します。
- 1回のHTML巡回上限を15,000ページに設定しました。
- 未処理が残った場合は残件数をログ表示します。
- 次回 `Fresh start = OFF` で、未処理キューの続きから再開します。
- `Fresh start = ON` は最初からやり直します。
- 各回終了時点のExcel/CSVをArtifactへアップロードします。

推奨:
1. 初回 `AUTO + URL_ONLY + Fresh start ON`
2. 未処理が残ったら `AUTO + URL_ONLY + Fresh start OFF`
3. `HTML巡回は完了しました。` が出るまで必要に応じて繰り返す


## v3.7 の変更点 — 累積URL出力の修正

v3.6で一部実行時に、Excel/CSVへ「今回処理した一部URL」しか出ない可能性があった点を修正しました。

### 重要な仕様
- XMLで発見したURLは、HTML巡回前に必ず全件を累積URL DBへ登録します。
- `max_html_pages_per_run = 15000` は「今回HTMLを確認するページ数」の上限だけです。
- Excel/CSVの件数上限ではありません。
- AUTOでは、現在DBに存在する全URLをHTML巡回キュー候補に入れます。
- 既に処理済みのURLは `html_queue` の状態で再処理されません。
- Fresh start OFFでは、前回までのURLを保持したまま続きから巡回します。
- Fresh start ONでは、そのサイトのDBを完全に作り直します。
- 出力直前に `[EXPORT] cumulative unique URLs = ...` をログへ表示します。

### 期待される例
XMLで 29,285 URL 発見し、今回HTMLで 1,200 URL 追加した場合、
HTML巡回が15,000ページで区切られても、Excel/CSVには約30,485件の累積URLが出力されます。

### 推奨運用
初回:
`AUTO + URL_ONLY + Fresh start ON`

続き:
`AUTO + URL_ONLY + Fresh start OFF`

ログの `[EXPORT] cumulative unique URLs` と、Excelの行数が一致しているか確認してください。


## v3.8 の変更点 — XML発見URLの速報ダウンロード

AUTOで実行すると、最初に `xml-preview` ジョブだけが動きます。

1. XMLサイトマップからURLを収集
2. XMLで見つかったURLだけのExcel / CSVを作成
3. `＜対象ホスト＞_XML発見URL_速報版` というArtifactをアップロード
4. `xml-preview` ジョブ完了後、本番のHTML巡回を開始

そのため、HTML巡回が数時間続いていても、XML分だけは先にダウンロードできます。

例:
- `www.insource.co.jp_XML発見URL_速報版`
- 中身: `www.insource.co.jp_url_only.xlsx` / `.csv`

### 注意
- 速報版はXMLサイトマップで見つかったURLのみです。
- ページタイトルは基本的に空欄です（各ページHTMLへアクセスしないため）。
- AUTOの最終Artifactには、HTML巡回で追加発見したURLや取得できたページタイトルも含まれます。
- XML_ONLYを選んだ場合は速報版だけ作成し、HTML巡回ジョブは実行しません。


## v3.9 修正版

v3.8でリアルタイム進捗表示時に発生した以下のエラーを修正しました。

`NameError: name 'format_duration' is not defined`

修正内容:
- `format_duration()` を追加
- `eta_clock()` を追加
- 経過時間・処理速度・残り時間・完了予想の表示を復旧
- Python構文チェックに加えて、必要な関数が実際に定義されているか静的チェックを実施
- v3.8のXML速報ダウンロード、累積URL出力、分割再開、タイトル取得などは維持
