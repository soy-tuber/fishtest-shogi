# 分散定石生成基盤 設計書（仮称: bookforge）

fishtest（official-stockfish/fishtest）をフォークし、「対局で検定する基盤」を「局面を探索して定石DAGを育てる基盤」に作り替える。
本書は Claude Code に渡す前提で、決定事項・データ構造・API・実装フェーズと受け入れ条件を記す。

> 実装上の判断・設計書との差分は [phase1-notes.md](phase1-notes.md)、fishtest の棚卸しは [phase0-inventory.md](phase0-inventory.md) を参照。

---

## 0. 決定事項（前提）

| 項目 | 決定 |
|---|---|
| ベース | fishtest をフォーク。サーバー骨格（FastAPI/uvicorn、RunDb、Scheduler、RunCache、タスク回収、ITP、worker の heartbeat/updater/sri）は流用する |
| コーディネータ | さくらVPS（Ubuntu）に常駐 |
| 公開経路 | Cloudflare Tunnel（cloudflared）。VPS はインバウンドポートを開けない |
| 認証 | Cloudflare Access。人間＝Access ログイン、ワーカー＝1台1枚のサービストークン。fishtest のパスワード認証は撤去 |
| 配布物 | エンジン・評価関数・DLモデルは R2（または GitHub Releases）。サーバーはハッシュとURLだけ渡す |
| ワーカー（初期） | ① Ryzen 9950X3D2 / Windows ネイティブ / NNUE　② RTX 5090 / WSL（将来 Linux 直） / DL |
| 初期局面 | 手動指定（SFEN・指し手列・棋譜＋手数範囲） |
| 評価値 | エンジン種別ごとに別カラム。NNUE の cp と DL の勝率を混ぜない |

### 0.1 fishtest から残すもの / 捨てるもの

| 残す | 置き換える | 捨てる |
|---|---|---|
| `server/fishtest/` の FastAPI 構成（`api.py` / `views.py` の2ルーター） | `games.py`（fastchess 対局）→ `search.py`（USI 探索） | SPRT/LLR 計算（Phase 6 で検定ランとして復活させる） |
| `RunDb` / `RunCache` / `Scheduler` / `scavenge_dead_tasks` / `update_itp` / `wtt_map` | run の `args` スキーマ（vtjson）→ 定石ラン用 | fastchess・Stockfish ソースのダウンロードとビルド |
| worker の heartbeat スレッド、`fish.exit` 停止、`updater.py` + `sri.txt` 完全性検査 | `validate_request()` の username/password 検証 → Access JWT 検証 | GitHub の SHA/ancestry 連携、PGN アップロード |
| ワーカー一覧・貢献者ランキング・run 一覧の画面構成 | run 詳細画面（LLR → 定石DAGの成長・展開状況） | SPSA サブシステム（将来必要なら戻す） |

**ライセンス**: フォーク元の LICENSE を確認して継承すること（Phase 0 のタスク）。

---

## 1. 全体構成

```
[Access ログイン(人間)]──┐
                        ├─ Cloudflare Edge (Access) ── Tunnel ── VPS: cloudflared → uvicorn(FastAPI)
[サービストークン(worker)]┘                                              ├─ MongoDB : users / workers / runs / tasks / actions
                                                                         └─ SQLite  : books/<book_id>.db（局面DAG）
[R2] engines/ evals/ models/  ← worker が直接取得（サーバーを通さない）
```

- **MongoDB は fishtest のまま残す**（runs/tasks/users/workers/actions）。RunDb を書き換えないため。
- **局面DAGは SQLite に分離**する。1つの「定石ブック（book）」＝1ファイル。後手定跡をツリー単位で分けて作る運用（例: 根ごとに別ブック）にそのまま対応でき、完成品を手元の SQLite 定跡ラッパーにそのまま持ち込める。
- run は必ずどれか1つの book に紐づく。

---

## 2. 概念モデル

- **Book**: 局面DAG（SQLite 1ファイル）。視点（どちらの手番を「自分」とするか）と展開ポリシーの既定値を持つ。
- **Run**: Book に対する作業単位。種類は4つ。
  - `expand` … 根から展開して DAG を育てる（主用途）
  - `reeval` … 既存局面を指定エンジン・指定探索量で再評価し直す（既存定跡の取り込み後に最新評価関数で深く評価し直す用途）
  - `dual` … 同じ局面を NNUE と DL の両方で評価し、見解割れを抽出する
  - `sprt` … （Phase 6）定石あり/なしの対局検定。fishtest の SPRT 部分を復活
- **Task**: Run から切り出した局面の束（lease 単位）。1 Task = 1 エンジン種別。
- **Worker**: 1プロセス＝1 Worker（UUID をローカル保存）。能力（`nnue` / `dl`）を申告。
- **User**: Access のメールアドレスで識別。ロールは `admin` / `approver` / `contributor`。

---

## 3. 局面DAG（SQLite: `books/<book_id>.db`）

WAL モード。書き込みはサーバープロセスの単一ライター（RunCache と同じく、結果はメモリで束ねてから定期フラッシュ）。

```sql
-- 局面。キーは「手数を除いた」正規化SFEN（盤面 手番 持ち駒）
CREATE TABLE node (
  id            INTEGER PRIMARY KEY,
  sfen          TEXT NOT NULL UNIQUE,     -- 手数なし正規化SFEN
  ply_min       INTEGER,                  -- 根からの最短手数（参考値。キーには使わない）
  status        INTEGER NOT NULL,         -- 0=pending 1=leased 2=evaluated 3=pruned 4=terminal
  priority      REAL NOT NULL DEFAULT 0,  -- 展開優先度（§5）
  value_nnue    INTEGER,                  -- 伝播後の値（手番側視点 cp）
  value_dl      REAL,                     -- 伝播後の値（手番側視点 勝率）
  dirty         INTEGER NOT NULL DEFAULT 0, -- 伝播が必要
  updated_at    INTEGER
);
CREATE INDEX node_queue ON node(status, priority DESC);

-- 探索結果（生データ。同一局面に複数あり得る）
CREATE TABLE eval (
  id            INTEGER PRIMARY KEY,
  node_id       INTEGER NOT NULL REFERENCES node(id),
  engine_kind   TEXT NOT NULL,            -- 'nnue' | 'dl'
  engine_hash   TEXT NOT NULL,            -- 実行バイナリの sha256
  eval_hash     TEXT NOT NULL,            -- 評価関数/モデルの sha256
  budget        INTEGER NOT NULL,         -- nnue=nodes, dl=playouts
  depth         INTEGER, seldepth INTEGER,
  worker_id     TEXT NOT NULL,
  task_id       TEXT NOT NULL,
  verified      INTEGER NOT NULL DEFAULT 0, -- 二重配布で照合済み
  created_at    INTEGER
);

-- 候補手（MultiPV）。eval ごと
CREATE TABLE cand (
  eval_id       INTEGER NOT NULL REFERENCES eval(id),
  rank          INTEGER NOT NULL,         -- MultiPV 順位
  move          TEXT NOT NULL,            -- USI 形式
  score_cp      INTEGER,                  -- nnue
  score_mate    INTEGER,
  winrate       REAL,                     -- dl
  pv            TEXT,
  PRIMARY KEY (eval_id, rank)
);

-- 辺（局面→子局面）。合流・循環があるので木ではなくDAG（循環も許容）
CREATE TABLE edge (
  parent_id     INTEGER NOT NULL REFERENCES node(id),
  move          TEXT NOT NULL,
  child_id      INTEGER NOT NULL REFERENCES node(id),
  PRIMARY KEY (parent_id, move)
);
CREATE INDEX edge_child ON edge(child_id);

CREATE TABLE root (
  node_id       INTEGER PRIMARY KEY REFERENCES node(id),
  label         TEXT,                     -- 例: "76歩34歩 系"
  source        TEXT                      -- 入力元（sfen / moves / kif）
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); -- 視点・ポリシー既定値・スキーマ版
```

- 正規化・合法手生成・指し手適用はサーバー側で `cshogi` を使う（**ワーカーの結果は必ずサーバーで盤面適用して検証**）。
- 千日手・循環は `terminal` 扱いにせず、伝播時に引き分け値で打ち切る（§6）。

---

## 4. Run の設定（vtjson スキーマで検証）

```jsonc
{
  "type": "expand",                 // expand | reeval | dual | sprt
  "book_id": "gote-76fu-34fu",
  "engine": {
    "kind": "nnue",                 // nnue | dl
    "family": "yaneuraou",          // 表示用
    "binaries": {                   // プラットフォーム別の配布物（DL は §8.3 のローカル上書きも許可）
      "windows-x64-avx512vnni": {"url": "...", "sha256": "..."},
      "linux-x64-avx2":          {"url": "...", "sha256": "..."}
    },
    "eval":   {"url": "...", "sha256": "..."},
    "usi_options": {"Hash": 512, "Threads": 1, "BookFile": "no_book"}
  },
  "search": {
    "budget": 20000000,             // nnue=nodes / dl=playouts
    "multipv": 4,
    "mode": "st"                    // st=1スレッド固定（決定的・照合可）/ smp=多スレッド（深い再評価用、照合は許容幅）
  },
  "policy": {                       // 展開ポリシー（§5）
    "self_side": "gote",            // 自分側の手番（sente|gote）。自分側は絞る・相手側は広げる
    "self_eval_diff": 0,            // 自分の手番: 最善からこの幅以内だけ展開（0=最善手のみ）
    "opp_eval_diff": 60,            // 相手の手番: 最善からこの幅以内を全部展開
    "max_ply_from_root": 40,
    "min_budget_for_expand": 5000000,
    "reeval_if_unstable_cp": 80     // 浅い評価と深い評価の差がこれを超えたら再評価
  },
  "stop": {
    "max_nodes": 5000000,
    "max_core_hours": 500,
    "until_frontier_empty": true
  },
  "priority": 0,                    // fishtest の priority を流用
  "throughput": 100,                // fishtest の throughput を流用（ITP計算に使う）
  "verify_ratio": 0.0               // 二重配布の割合。自分のマシンだけの間は 0、チーム参加後 0.02
}
```

- `dual` は `engine` を2つ（nnue と dl）持ち、Task を2系統に分けて発行する。
- `reeval` は `policy` の代わりに対象範囲（全局面 / ply 範囲 / 最終評価時の budget 未満 など）を持つ。

---

## 5. 展開ポリシーと優先度（中央の「評価」）

### 5.1 子局面の生成（結果受理時）

評価済み局面 `n` の MultiPV 候補から子を作る。`n` の手番が
- **自分側**（`self_side`）: 最善手から `self_eval_diff` 以内の手だけ展開
- **相手側**: 最善手から `opp_eval_diff` 以内の手をすべて展開

`self_side` を切り替えるだけで先手用・後手用の両方を作れる（ペタショック型の非対称展開を、どちらの手番にも向けられる形に一般化したもの）。

### 5.2 優先度スコア（pending 局面の並べ替え）

```
priority = w_line  * (1 / (1 + ply_from_root))           // 根に近いほど優先
         + w_close * exp(-|Δ最善| / τ)                    // 親から見て最善に近い手ほど優先
         + w_freq  * log(1 + 実戦出現数)                   // floodgate 等の出現頻度（任意・後付け）
         + w_split * 1[NNUE と DL の見解割れ]              // dual ランの成果
         + w_unst  * 1[再評価で値が大きく動いた]
```

係数は run 単位で上書き可能。初期値はコード中の定数でよい。

### 5.3 終了条件

`stop` のいずれかに到達、または展開幅の中に pending が無くなった時点で run を `finished` にする（fishtest の「統計的に有意になったら止める」を置き換える箇所）。

---

## 6. 値の伝播（ネガマックス）

- 各局面の値 = 子の値（符号反転）の最大。子が未評価なら、その局面自身の探索値で代用。
- 循環があるので再帰ではなく、`dirty` フラグを持つ局面から親方向へキューで反復緩和する。1パスで収束しない場合は反復回数上限で打ち切る。
- 千日手（同一局面への回帰）は引き分け値（0cp / 勝率0.5）。
- NNUE と DL は別々に伝播させる（`value_nnue` / `value_dl`）。
- Scheduler に `propagate_dirty`（例: 30秒ごと）を追加する。

---

## 7. API（fishtest のエンドポイント名を流用）

すべて JSON の POST。認証は §9 のミドルウェアで済ませ、ハンドラは `request.state.principal`（user_id / worker_token_id）を読むだけにする。

| エンドポイント | fishtest での役割 | 本基盤での役割 |
|---|---|---|
| `/api/request_version` | 資格情報確認・必要バージョン | ワーカー登録（UUID・HW情報・ベンチNPS・能力）と最低バージョン返却 |
| `/api/request_task` | タスク確保 | 能力とスロット数に合う Task（局面の束）を lease |
| `/api/beat` | 生存通知 | 生存通知＋進捗。応答に `continue:false` が来たら打ち切り |
| `/api/update_task` | 結果の逐次送信 | 探索結果の逐次送信（部分送信可） |
| `/api/failed_task` | 失敗通知 | 同じ（エンジン異常終了など） |
| `/api/worker_log` | ログ送信 | 同じ |

### 7.1 request_version

```jsonc
// req
{"worker_uuid":"…","version":1,"sri":"<sha384>","capabilities":["nnue"],
 "hw":{"os":"windows","cpu":"Ryzen 9 9950X3D2","threads":32,"flags":["avx2","avx512vnni"],
       "gpus":[],"cuda":null,"tensorrt":null},
 "bench":{"nnue_nps_per_thread":0}}
// res
{"worker_id":"…","min_version":1,"accepted":true,"message":""}
```

### 7.2 request_task

```jsonc
// req
{"worker_id":"…","capability":"nnue","slots":30}
// res（無ければ {"task": null, "retry_after": 60}）
{"task":{
  "task_id":"…","run_id":"…","lease_expires":"…",
  "engine":{"kind":"nnue","binary":{"url":"…","sha256":"…"},"eval":{"url":"…","sha256":"…"},
            "usi_options":{"Hash":512,"Threads":1}},
  "search":{"budget":20000000,"multipv":4,"mode":"st"},
  "positions":[{"pos_id":123,"sfen":"…"}, …]
}}
```

Task の大きさは「1 lease ≒ 10分」を目安に、ワーカーのベンチNPSとスロット数から決める。

### 7.3 update_task

```jsonc
{"task_id":"…","final":false,
 "results":[{"pos_id":123,"engine_hash":"…","eval_hash":"…","budget":20000000,
             "depth":38,"seldepth":52,"elapsed_ms":41000,
             "multipv":[{"move":"7g7f","score_cp":45,"pv":"7g7f 3c3d …"}, …]}]}
// res
{"accepted":[123],"rejected":[{"pos_id":124,"reason":"illegal_move"}],"continue":true}
```

受理時の検査: ハッシュ一致 → budget 一致 → cshogi で合法手・PV の適用確認 → （二重配布対象なら）照合キューへ。

### 7.4 タスクの期限切れ

fishtest の `scavenge_dead_tasks` をそのまま使う。heartbeat が途絶えた Task の未完了局面は `pending` に戻す。

---

## 8. ワーカー

fishtest の `worker/` をフォークし、`games.py` を `search.py` に置き換える。制御の骨格（`worker()` → `fetch_and_handle_task()` ループ、heartbeat スレッド、`fish.exit`、`updater.py` + `sri.txt`）は残す。

### 8.1 共通の動き

1. 起動: 設定読込 → HW検出 → 短いベンチ → `request_version`
2. `request_task`（能力・スロット数を添えて）
3. 配布物: キャッシュ（`cache/<sha256>`）に無ければ R2 から取得し、sha256 を検証
4. 探索: USI でエンジンを起動 → `setoption` → 各局面で `position sfen …` → `go nodes N`（DL は playouts 指定）→ `info`（MultiPV）を回収
5. N 件ごとに `update_task`、heartbeat は 60 秒ごと
6. 稼働時間帯外・`fish.exit` 検出・SIGTERM → 実行中の一塊を送ってから終了

設定ファイル `bookforge.cfg`（`.gitignore` 対象、権限 600）:

```ini
[server]
url = https://bookforge.<your-domain>
cf_access_client_id = …
cf_access_client_secret = …
[worker]
capability = nnue          ; nnue | dl
slots = 30
hours = 00:00-24:00
priority = below_normal    ; OS のプロセス優先度
[dl]
gpu = 0
local_engine = /opt/dlshogi/usi   ; §8.3
```

### 8.2 Windows ネイティブ（9950X3D2 / NNUE）

- Python 3.12 を Windows に直接入れる（MSYS2 不要。ビルドしないため）。
- 並列方式は `mode=st`: **1スレッドのエンジンプロセスを slots 個並べる**。決定的になり照合できる。`Hash` はプロセスごと（例 512MB × 30）。
- `mode=smp`（深い再評価用）では 1プロセス多スレッドにする。
- プロセス優先度は `BELOW_NORMAL_PRIORITY_CLASS`。自分の作業や大会用の対局を邪魔しない。
- 常駐はタスクスケジューラ（ログオン時起動）または NSSM でサービス化。
- AVX-512 VNNI 版バイナリを配布物の1つとして用意し、ワーカーが CPU フラグで選ぶ。

### 8.3 WSL（RTX 5090 / DL）

- WSL で systemd を有効化し、ワーカーを systemd サービスにする。WSL はアイドルで VM が止まるので、常駐させる設定（`.wslconfig` 側の VM アイドル停止の抑止など）を入れる。
- **DL はローカルエンジンの上書きを許可**する。TensorRT エンジンはマシン依存で自前ビルドが前提なので、バイナリは配布せず、`local_engine` の sha256 を毎回申告させる。ただし**モデル（評価関数）の sha256 は run の指定と一致必須**。
- 並列方式は 1プロセス・大バッチ。`slots` は「同時に投げる局面数」として扱う。
- 将来 Linux 直に移行しても、同じコードで動くこと（WSL 固有の処理は起動スクリプト側に閉じる）。

---

## 9. 認証（Cloudflare Access）

### 9.1 Access の設定

- **アプリ1（人間用）**: `bookforge.<domain>/*`。ポリシー = 許可するメールアドレス（またはGitHub組織）。これが fishtest の「アカウント承認」に相当する。
- **アプリ2（ワーカー用）**: `bookforge.<domain>/api/*`。ポリシーのアクションを Service Auth にし、発行したサービストークンだけを許可する。
- トークンは 1 ワーカー 1 枚。失効は Access の管理画面から1台単位で行う。

### 9.2 サーバー側の検証（必須）

Tunnel 経由でしか到達できない構成でも、**オリジンで JWT を検証する**（ヘッダの偽装対策）。

1. `Cf-Access-Jwt-Assertion` ヘッダを取り出す
2. チームドメインの公開鍵（certs エンドポイント）で署名を検証し、`aud` がアプリの AUD タグと一致することを確認
3. 人間なら `email` クレーム → `users` を引く。サービストークンなら `common_name`（クライアントID）→ `worker_tokens` を引く
4. `request.state.principal` に格納。`/api/*` でサービストークン以外、画面側で人間以外はそれぞれ拒否

`validate_request()` の username/password 処理はこのミドルウェアに置き換える。

### 9.3 権限

| 操作 | admin | approver | contributor | worker |
|---|---|---|---|---|
| book 作成・根局面登録 | ○ | ○ | – | – |
| run 作成 | ○ | ○ | ○（承認待ちに入る） | – |
| run 承認 | ○ | ○ | – | – |
| ワーカートークン管理 | ○ | – | – | – |
| request_task / update_task / beat | – | – | – | ○ |

fishtest の「Pending approval」の列をそのまま使う。

---

## 10. 画面（views.py を改修）

- **トップ**: 上部に稼働状況（稼働ワーカー数 / NNUE nps 合計 / DL playouts/s 合計 / 残り見込み時間）。その下に Workers・Pending approval・Paused・Failed・Active・Finished の折りたたみ（fishtest と同じ）。
- **ワーカー一覧**: マシン名・能力（NNUE/DL）・スレッド数/GPU・UUID・nps・担当 run。
- **run 詳細**: 展開済み局面数、pending 数、深さ分布、評価値の分布、直近1時間の処理速度、担当ワーカー、根ごとの伝播値。
- **book 詳細**: 根局面の一覧（盤面プレビュー付き）、エクスポートボタン。
- **根局面の登録フォーム**: SFEN（複数行）/ 平手からの指し手列 / KIF・CSA 貼り付け＋手数範囲。登録前に盤面プレビューを出し、重複は正規化SFENで弾く。
- **貢献者ランキング**: ユーザー別・エンジン種別別（NNUE nodes / DL playouts を別列。換算して1本にしない）。

---

## 11. 入出力

- **取り込み**: やねうら王定跡形式、および手元の SQLite 定跡（スキーマ変換スクリプト）から node/eval/cand/edge へ。`reeval` の対象として使う。
- **書き出し**: やねうら王定跡形式（候補手・評価値・深さ付き）と、手元の SQLite 定跡ラッパーのスキーマ。伝播後の値（`value_nnue`）を採用値とする。

---

## 12. 実装フェーズと受け入れ条件

### Phase 0: フォークと棚卸し
- LICENSE の確認と継承、README 差し替え
- fastchess・SPRT・SPSA・PGN・GitHub 連携の呼び出し箇所を洗い出し、無効化（削除は Phase 1 以降）
- **受け入れ**: サーバーが MongoDB だけで起動し、既存テストのうち残すものが通る

### Phase 1: サーバー MVP（ローカル）
- run スキーマ（§4）、book の SQLite（§3）、根局面登録（SFEN と指し手列）
- `request_version` / `request_task` / `beat` / `update_task` / `failed_task`（§7）
- 受理時検査（ハッシュ・budget・cshogi での合法性）、子局面生成（§5.1）、伝播（§6）
- 開発中は認証をスタブ（固定 principal）にしておく
- **受け入れ**: ダミーワーカー（ランダム合法手＋乱数評価値を返すスクリプト）で 1 run を最後まで回し、終了条件で `finished` になる。伝播値が手計算と一致する小さな DAG のテストがある

### Phase 2: NNUE ワーカー（Windows / 9950X3D2）
- `search.py`（USI ドライバ、MultiPV 回収、`st` / `smp`）
- 配布物キャッシュと sha256 検証、`fish.exit`、プロセス優先度
- **受け入れ**: 根1つから `max_ply_from_root=6` 程度の run を完走。同じ局面を2回投げて `st` モードの結果が一致する

### Phase 3: VPS 配備と認証
- cloudflared（Tunnel）、Access アプリ2つ、JWT 検証ミドルウェア（§9.2）
- systemd で uvicorn と MongoDB を常駐、SQLite のバックアップ（日次で R2 へ）
- **受け入れ**: サービストークン無しで `/api/*` が 403。トークンを失効させると即座に request_task が拒否される

### Phase 4: DL ワーカー（WSL / RTX 5090）と dual ラン
- ローカルエンジン上書き（§8.3）、モデル sha256 の一致検査
- `dual` ラン、見解割れの抽出と優先度への反映
- **受け入れ**: 同じ根に対して NNUE と DL の評価が両方付き、割れた局面の一覧が画面に出る

### Phase 5: 画面と運用
- §10 の画面、根局面の KIF/CSA 取り込み、エクスポート（§11）、取り込みと `reeval`
- 二重配布の照合（`verify_ratio`）と、不一致が多いワーカーの自動隔離
- **受け入れ**: 既存定跡を取り込み、`reeval` で最新評価関数による再評価を完走し、エクスポートした定跡を手元のラッパーで読める

### Phase 6: 検定（任意）
- fishtest の SPRT を `sprt` ランとして復活。定石あり/なしの対局で効果を判定

---

## 13. Claude Code への作業指示（リポジトリ直下の CLAUDE.md に転記する想定）

- 1フェーズずつ進め、各フェーズの「受け入れ」を満たすテストを先に書く
- fishtest 由来のコードは、置き換える前に「何を呼んでいるか」を一覧化してから触る。削除は動作確認後
- 局面の同一性は常に「手数を除いた正規化SFEN」で扱う。手数をキーに混ぜない
- NNUE と DL の評価値を同じカラムに入れない。換算もしない
- ワーカーは判断しない。展開・優先度・終了判定はすべてサーバー側に置く
- 秘密情報（Access のクライアントシークレット等）はコード・テスト・ログに出さない
