# Site URL Crawler

GitHub Actions の無料枠でも使いやすいように、**1回の実行で全件を無理にクロールせず、途中保存しながら分割して再開できる**サイト内URL収集ツールです。

## できること

- `config.json` の `target_url` を起点に内部リンクを巡回
- 外部ドメインを除外
- URL重複を除外
- 画像 / PDF / CSS / JS / Officeファイルなどを除外
- HTMLページのみExcel・CSVへ保存
- title / h1 / status code / content-type / canonical / robots meta を取得
- 第1階層・第2階層を自動抽出
- クロール深度と発見元URLを保存
- 429 / 5xx をリトライ
- `robots.txt` を尊重
- GitHub Actionsの途中終了に備えてstateを保存
- 次回実行時に続きから再開
- ActionsのArtifactからExcel/CSVをダウンロード可能

---

## 1. GitHubへアップロード

このフォルダ一式を新しいGitHubリポジトリへアップロードしてください。

必要ファイル:

```text
site-url-crawler/
├─ crawler.py
├─ config.json
├─ requirements.txt
├─ README.md
├─ .gitignore
├─ output/
├─ state/
└─ .github/
   └─ workflows/
      └─ crawl.yml
```

---

## 2. 対象URLを設定

`config.json` を開きます。

```json
{
  "target_url": "https://www.insource.co.jp/",
  "max_pages_per_run": 5000,
  "delay_seconds": 0.7
}
```

別サイトを調査するときは `target_url` を変更するだけです。

例:

```json
"target_url": "https://school.recruit-ms.co.jp/"
```

---

## 3. GitHub Actionsを実行

GitHubで、

**Actions → Crawl site URLs → Run workflow**

を開きます。

最初は `fresh_start = false` のままで実行してください。

クロールが途中まで進むと `state/` に進捗が保存されます。

もう一度 `Run workflow` を押すと、前回の続きから再開します。

---

## 4. 約3万ページのサイトを調査する場合

初期設定は、

```json
"max_pages_per_run": 5000
```

です。

29,000ページなら、単純計算では6回前後に分けてクロールします。

サイト構造によっては発見URL数・実行回数は増減します。

無料枠を節約したい場合は、

```json
"max_pages_per_run": 3000
```

などに下げても構いません。

---

## 5. 出力ファイル

`output/` に以下が作成されます。

```text
output/
├─ www.insource.co.jp_urls.xlsx
├─ www.insource.co.jp_urls.csv
└─ www.insource.co.jp_summary.json
```

Excelには次のシートがあります。

### URL一覧

- url
- title
- h1
- status_code
- content_type
- canonical
- robots_meta
- depth
- discovered_from
- first_directory
- second_directory
- response_ms
- final_url
- fetched_at
- error

### 集計

- 取得HTML URL数
- 200件数
- 3xx件数
- 4xx件数
- 5xx件数
- エラー件数

### 第1階層集計

例:

```text
/bup/               8500
/kyoiku/            6200
/dougahyakkaten/    2100
/consulting/         500
```

---

## 6. Excelをダウンロード

Actionsの実行が終わったら、実行結果画面下部の **Artifacts** に

```text
crawl-output-○○
```

が表示されます。

そこからZIPをダウンロードできます。

また、最新版はリポジトリの `output/` にも保存されます。

---

## 7. 続きから再開

このツールでは `state/` に、

- 取得済みURL
- 未巡回URL
- 取得済みデータ

を保存します。

そのためGitHub Actionsの1回の実行で終わらなくても、次回実行時に続きから再開できます。

---

## 8. 最初からやり直す

Actions → Run workflow で

```text
fresh_start = true
```

を選択します。

保存済みstateを削除してトップページから再クロールします。

---

## 9. 自動実行

`.github/workflows/crawl.yml` には1日1回のscheduleも入っています。

```yaml
schedule:
  - cron: "20 18 * * *"
```

これはUTCです。日本時間では翌日03:20頃です。

不要なら `schedule:` の2行を削除してください。

自動実行の場合もstateから続きを処理するので、数日かけて大規模サイトを収集できます。

---

## 10. 相手サイトへの負荷について

初期値は、

```json
"delay_seconds": 0.7
```

としてあります。

短くしすぎないことをおすすめします。

また、このツールは `robots.txt` を確認し、クロール不可と指定されたURLにはアクセスしない設定です。

```json
"respect_robots_txt": true
```

基本的には `true` のまま使用してください。

---

## 11. URLパラメータについて

初期状態ではクエリパラメータを削除します。

```json
"keep_query": false
```

これにより、

```text
/page/?utm_source=xxx
/page/?utm_source=yyy
```

を同一ページとして扱いやすくしています。

検索結果や絞り込みURLも取得したい場合だけ `true` にしてください。

---

## 12. サブドメイン

初期状態では対象ホストと完全一致するURLだけを取得します。

```json
"include_subdomains": false
```

たとえば `www.example.com` から `shop.example.com` まで巡回したい場合は `true` にします。

---

## 13. GitHubの設定でpushが失敗する場合

リポジトリの

**Settings → Actions → General → Workflow permissions**

で、

**Read and write permissions**

を選択してください。

この権限は `state/` と `output/` を次回へ残すために使用します。

---

## 注意

このツールが収集するのは、開始ページからリンクをたどって発見できるURLです。

以下のようなURLは自動では発見できない場合があります。

- どこからもリンクされていない孤立ページ
- JavaScript実行後にしか生成されないURL
- ログイン後のみ表示されるURL
- robots.txtでクロール禁止されているURL
- フォーム送信後のみ現れるURL

そのため「Webサーバー上に存在する全ファイル」と完全一致することを保証するものではありません。

競合サイトの公開URL調査では、HTML内部リンククロール + XMLサイトマップ取得を組み合わせるとさらに網羅率を上げられます。
