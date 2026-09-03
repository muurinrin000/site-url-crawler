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
