# Phase 1: サーバー MVP — 実装メモ

## 構成

`server/bookforge/`（fishtest とは別パッケージ。fishtest からは `fishtest.scheduler.Scheduler` だけを流用）

| モジュール | 役割 | 設計書 |
|---|---|---|
| `shogi.py` | 正規化SFEN（手数なし）、指し手適用、合法性・PV 検査、終局判定（詰み・入玉宣言）。cshogi に渡す前に文字列を正規表現で検査する（cshogi は不正 SFEN でプロセスごと abort することがあるため） | §3 |
| `bookdb.py` | `books/<book_id>.db`（SQLite, WAL）。1 接続＋ロックで単一ライター | §3 |
| `values.py` | cp / 勝率の扱い。詰みスコア→cp（±32000 − 手数）、DL の符号反転は `1 − w` | §0, §6 |
| `policy.py` | 非対称展開（§5.1）と優先度（§5.2） | §5 |
| `propagate.py` | ネガマックス伝播。`propagate_dirty`（dirty ノードとその祖先だけ）/ `propagate_all` | §6 |
| `schemas.py` | run 設定・ワーカー API の vtjson スキーマ | §4, §7 |
| `validate.py` | 結果の受理検査（ハッシュ → budget → 候補手と PV を盤面に適用） | §7.3 |
| `service.py` | run / task / worker の管理（MongoDB）、lease、終了判定、dead task 回収、Scheduler 登録 | §2, §5.3, §7 |
| `auth.py` | `request.state.principal` を付けるミドルウェア（Phase 1 はスタブ） | §9 |
| `api.py`, `app.py` | FastAPI ルーターとアプリ | §7 |
| `cli.py` | 管理 CLI（book 作成・根登録・run 作成/承認/一時停止・状態表示・伝播） | – |
| `dummy_worker.py` | ランダム合法手＋乱数評価のダミーワーカー | §12 |

## 受け入れ条件との対応

| 条件 | テスト |
|---|---|
| ダミーワーカーで 1 run を最後まで回し `finished` になる | `tests/test_bookforge_e2e.py::TestExpandRun.test_run_to_completion`（frontier 枯渇で終了）、`test_max_nodes_stops_run` |
| 伝播値が手計算と一致する小さな DAG | `tests/test_bookforge_propagate.py`（3 手の木、合流、詰み、千日手ループ、ループからの脱出、勝率、SQLite 経由の増分伝播＝全再計算） |

さらに実 HTTP（uvicorn）＋ `python -m bookforge.dummy_worker` でも、根 2 つ・`max_ply_from_root=5` の run が完走することを確認した（MongoDB の代わりに mongomock を使用）。

## 設計書からの差分・解釈（要確認）

1. **fishtest の RunDb を書き換えず、別コレクションを使う。** run / task / worker は `book_runs` / `book_tasks` / `book_workers` / `book_worker_logs`。fishtest の `runs` スキーマ検証（`validate_data_structures`）と混ざらないようにするため。Task は run 文書に埋め込まず別コレクション（数百万局面規模で 16MB 制限に当たるため。§1 の「tasks」コレクションに相当）。
2. **run の状態は `state` 1 フィールド**（`pending_approval` / `active` / `paused` / `finished` / `failed`）。fishtest の真偽値群ではなく、画面の折りたたみ区分（§10）にそのまま対応させた。
3. **`eval` テーブルに `elapsed_ms` と `run_id` を追加**（コア時間集計と追跡用）。索引 `eval(node_id, engine_kind)` と `node(dirty)` の部分索引も追加。
4. **`dual` ランの形**: `"engines": {"nnue": {...}, "dl": {...}}` と `"search": {"nnue": {...}, "dl": {...}}`（budget の単位が nodes と playouts で違うため search も種別ごと）。
5. **DL の展開幅は勝率単位で別指定**: `policy.self_winrate_diff` / `opp_winrate_diff`（既定 0.0 / 0.05）。cp の `self_eval_diff` を勝率に換算しない（§13）。
6. **`reeval` の対象範囲**: `"target": {"ply_min": …, "ply_max": …}`。「未完了」＝同じ種別・同じ `eval_hash`・budget 以上の eval がまだ無い局面。
7. **`expand` の「未完了」**: 同じ種別で budget ≥ `min_budget_for_expand` の eval がまだ無い、`ply_min ≤ max_ply_from_root` の局面。承認時に既存評価済み局面へ展開ポリシーを当てる（`seed_expand`）ので、取り込み済みの定跡からも続けて育てられる。スキーマで `search.budget ≥ min_budget_for_expand` を強制（満たさないと何も展開しない run になるため）。
8. **lease はメモリ上の集合**（run × 種別ごと）。`node.status=1 (leased)` は使っていない。複数 run が同じ book を触っても互いの lease を壊さないため。起動時に active な task から再構築する。
9. **1 ワーカー 1 task**。同じワーカーが再度 request_task すると前の task は破棄され、未完了局面は戻る（再起動したワーカー向け）。
10. **Task の大きさ**: `st` は `slots × floor(600 × nps / budget)`、`smp` は `600 × nps × slots / budget`、DL は `max(slots, 600 × playouts/s / budget)`。上限 5000。ベンチが無ければ `slots × 2`。
11. **バイナリ選択はサーバー側**: 同じ OS のうちワーカー申告の CPU フラグで動く最上位（avx512vnni > avx512 > avxvnni > bmi2 > avx2 > …）。DL で `local_engine: true` かつワーカーが `local_engine_sha256` を申告した場合は配布せず、その値を engine_hash として照合。
12. **千日手**: ループ（強連結成分）内は引き分け値から反復して解く。両者が繰り返しを選ぶなら 0 / 0.5、どちらかに有利な脱出があればその値になる。反復上限 64。
13. **子が未評価の手**: その手の値は親自身の MultiPV スコアで代用（ノード単位ではなく手単位）。展開されていない候補手も親の MultiPV スコアで比較に参加する。
14. **エンジン秒**: `max_core_hours` は `elapsed_ms × Threads` の累計で判定。DL は GPU 時間の目安として同じ式。
15. **書き込みは受信ごとに 1 トランザクション**（§3 の「メモリで束ねて定期フラッシュ」はまだ入れていない）。ワーカー数台の規模では十分。伝播は 30 秒ごとに Scheduler から。
16. **ワーカー版数**: `MIN_WORKER_VERSION = 1`（新ワーカー系列として 1 から）。

## 未実装（次フェーズ以降）

- `reeval_if_unstable_cp` と §5.2 の `w_unst` / `w_split` の実データ反映（関数は用意済み、入力が未接続）
- 伝播で最善手が未展開の手に移ったときの自動展開
- `verify_ratio`（二重配布の照合）
- Cloudflare Access JWT 検証（`BOOKFORGE_AUTH=access` は起動拒否にしてある）
- fishtest 側画面への統合、fishtest ワーカー API の置き換え
- 取り込み・エクスポート（§11）

## 開発手順

```bash
cd server
uv sync --group test
# テスト（MongoDB 不要。mongomock を使う）
uv run python -m unittest discover -s tests -p "test_bookforge_*.py"

# ローカルサーバー（MongoDB が localhost に必要）
BOOKFORGE_AUTH=stub BOOKFORGE_BOOKS_DIR=./books uv run uvicorn bookforge.app:app --port 8000
uv run python -m bookforge.cli create-book gote-76fu-34fu --self-side gote
uv run python -m bookforge.cli add-roots gote-76fu-34fu --moves "7g7f 3c3d"
uv run python -m bookforge.cli create-run run.json --approve
uv run python -m bookforge.dummy_worker --url http://127.0.0.1:8000 --once
uv run python -m bookforge.cli status <run_id>
```
