# Site URL Collector v3

## Actions
Target URL / Collection mode / Output mode / Concurrency / Requests per second / Fresh start の6項目です。

Collection mode:
- AUTO: XML＋HTMLで可能な限り収集
- XML_ONLY: XMLだけ高速収集
- HTML_CRAWL: HTML内部リンクから収集

Output mode:
- URL_ONLY: URL・発見経路・発見元URL・第1階層・第2階層
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
