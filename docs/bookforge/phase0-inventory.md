# Phase 0: fishtest の棚卸し

対象: フォーク時点の HEAD `2e54019`。行番号はこのコミット基準。
凡例: **K**=残す / **R**=置き換える / **D**=捨てる。

## 0. 先に判断が必要な事項

### LICENSE が存在しない

リポジトリ内に `LICENSE` / `COPYING` のいずれも無く、`pyproject.toml` にも license 表記が無い（あるのは `AUTHORS` のみ）。
設計書 §0.1 の「フォーク元の LICENSE を確認して継承」は **継承すべきライセンスが無い** という結果になった。

ライセンス表記の無いコードは、原則として著作者が全権利を保持している扱いになる。本フォークを非公開・個人利用に留めるなら実務上の問題は小さいが、
チームへの配布や公開を予定しているなら、Phase 1 以降の公開前に次のいずれかを決める必要がある（法的助言ではない。判断は上流と相談のうえで）。

1. 上流（official-stockfish/fishtest）のメンテナにライセンスを確認する
2. fishtest 由来コードを使わない構成に寄せる（bookforge パッケージは既に fishtest の `Scheduler` 以外を import していない）

→ 本コミットでは LICENSE ファイルを **追加していない**（推測でライセンスを付けない）。

### 既存のパスワード認証は平文比較

`server/fishtest/userdb.py:44-79` `UserDb.authenticate` はパスワードを平文で比較しており（`user.get("password") != password`）、ワーカーは全リクエストの JSON にパスワードを載せる（`api.py:89-102`）。
設計書どおり Phase 3 で Cloudflare Access に置き換えるまで、fishtest 側のアカウントを外部公開しないこと。

## 1. fastchess / Stockfish のダウンロード・ビルド・実行（ワーカー側が中心）

| 場所 | シンボル | 内容 | 判定 |
|---|---|---|---|
| worker/worker.py:75-78 | `FASTCHESS_SHA`, `WORKER_VERSION=329`, `FILE_LIST` | 版数は api.py:20 と sri.txt にも重複 | R |
| worker/worker.py:379, 426-515 | `verify_fastchess`, `setup_fastchess` | GitHub zipball 取得 → make → テスト | R |
| worker/worker.py:1007-1124 | `gcc_version` … `verify_toolchain` | SF/fastchess ビルド用ツールチェーン検査 | R（ビルドしないので不要） |
| worker/worker.py:1171 | `get_worker_arch` | Stockfish の `get_native_properties.sh` を取得・実行 | R（CPU フラグ検出に置換） |
| worker/worker.py:1278-1460 | `fetch_and_handle_task` | request_task → run_games → failed/stop → PGN | R（骨格は K） |
| worker/games.py:841-970 | `setup_engine` | エンジンソース取得・`make profile-build` | R（配布物キャッシュ＋sha256 検証へ） |
| worker/games.py:1033-1864 | `parse_fastchess_output`, `launch_fastchess`, `run_games` | 対局実行と結果集計 | R（`search.py` へ） |
| server/fishtest/rundb.py:193 | `update_books` | official-stockfish/books の books.json を 900 秒ごとに取得 | R |
| server/fishtest/util.py:335-626 | `estimate_game_duration`, `get_tc_ratio`, `get_hash` など | チェスの TC / Hash 解釈 | R |

## 2. SPRT / LLR / Elo

| 場所 | シンボル | 判定 |
|---|---|---|
| server/fishtest/stats/*.py | `SPRT_elo`, `LLR*`, `Brownian` など | D（Phase 6 で `sprt` ランとして復活） |
| server/fishtest/schemas.py:539-693, 768-795 | `compute_results`, `compute_flags`, `is_undecided`, run の `sprt?` | R（`is_undecided` は §5.3 の終了条件に置換） |
| server/fishtest/rundb.py:1327 / 1363 / 1752-1863 / 1960 | `calc_itp`（LLR ボーナス）, `worker_cap`, `sync_update_task`, `purge_run` | R / R / R / D |
| server/fishtest/api.py:458, 485 | `/api/get_elo`, `/api/calc_elo` | D |
| views / templates / static js | `tests_live_elo`, `sprt_calc`, `tests_stats`, `sprt.js`, `live_elo.js` など | D/R |

## 3. SPSA

`spsa_handler.py`, `spsa_workflow.py`, `/api/request_spsa`, schemas の `spsa?`, rundb の SPSA 分岐, `spsa*.js`, 関連テンプレート, worker/games.py の SPSA ループ — **すべて D**（設計書 §0.1）。

## 4. PGN

| 場所 | シンボル | 判定 |
|---|---|---|
| server/fishtest/api.py:274, 588, 606 | `upload_pgn`, `download_pgn`, `download_run_pgns` | D（ストリーミングの実装パターンはエクスポートで流用可） |
| server/fishtest/rundb.py:95, 725-745 | `pgndb`, `upload_pgn`, `get_pgn`, `get_run_pgns` | D |
| server/utils/create_pgndb.py, purge_pgns.py | 保守スクリプト | D |
| worker/worker.py:1415-1459 | `upload_pgn_data` | D |

## 5. GitHub 連携

`server/fishtest/github_api.py` 全体（SHA / ancestry / rate limit / master SHA）、`app.py:166-169` の `gh.init`、`rundb.py` の `tests_repo` 正規化と `near_github_api_limit`、`views_run.py` のブランチ解決、`/api/rate_limit`、`rate_limits` 画面 — **D/R**。
ワーカーの `download_sri` と `updater.py:13 WORKER_URL` は official-stockfish を指しているので、**フォーク先に向け直して K**。

## 6. ユーザー名/パスワード認証

| 場所 | シンボル | 判定 |
|---|---|---|
| server/fishtest/userdb.py:44 | `authenticate`（平文比較） | R（Access JWT） |
| server/fishtest/api.py:89-102, 115 | `validate_username_password`, `validate_request` | R（principal ミドルウェア） |
| server/fishtest/views.py:599-822 | `login`, `logout`, `signup`（zxcvbn） | R（Access ログイン） |
| server/fishtest/http/cookie_session.py, session_middleware.py, csrf.py | Cookie セッション・CSRF | K |
| worker/worker.py:321, 346 | `verify_credentials`, `get_credentials` | R（サービストークン） |

## 7. NN（評価関数ネット）のアップロード・配布

`nndb`, `/api/nn/{id}`, `/upload`, `/nns`, `views_run.get_nets`, `aws_nets_sync.py` — **D/R**（配布物は R2 + sha256 に一本化。設計書 §0）。

## 8. そのまま流用できる汎用部品

| 場所 | シンボル | fishtest 固有の依存 | 判定 |
|---|---|---|---|
| server/fishtest/scheduler.py | `Scheduler`, `Task` | なし（標準ライブラリのみ） | **K（bookforge が既に利用）** |
| server/fishtest/run_cache.py | `RunCache` | `run["cores"]`, `run["finished"]`, `cache_schema` | K（フィールド名を合わせれば可） |
| server/fishtest/rundb.py:831 | `scavenge_dead_tasks` | `handle_crash_or_time` が `task["stats"]` を読む | K（crash 集計を外す） |
| server/fishtest/rundb.py:288, 1327 | `update_itp`, `calc_itp` | `args.tc`, `args.threads`, SPRT | K / R |
| server/fishtest/rundb.py:103, 300, 311 | `wtt_map` 系 | `worker_name(worker_info)` | K |
| worker/worker.py:1198 | heartbeat スレッド | 120 秒周期（設計書は 60 秒） | K |
| worker/worker.py:1621-1642 | `fish.exit` | なし | K |
| worker/worker.py:228-320, updater.py, sri.txt | sri 完全性検査・自己更新 | 取得元 URL が official-stockfish | K（向け直し） |
| worker/games.py:43-340 | ログ・キャッシュ・HTTP ヘルパ | なし | K |

## 9. 既存テストと MongoDB 依存

CI（`.github/workflows/server.yaml`）は MongoDB 8.3 を起動して `unittest discover -s tests` を回す。

| テスト | 触るサブシステム | Mongo | 判定 |
|---|---|---|---|
| test_api.py | ワーカー API 全般、SPSA、PGN、get_elo、自動 purge | 要 | R |
| test_rundb.py | new_run, update_task, tests_repo, SPSA | 要 | R |
| test_github_api.py | GitHub（実ネットワーク＋GH_TOKEN） | 要 | D |
| test_nn.py / test_spsa_workflow.py / test_views_stats.py | NN / SPSA / SPRT 統計 | 要/不要/要 | D |
| test_users.py | ログイン・signup・パスワード | 要 | R |
| test_views_detail.py / test_views_tests.py / test_views_run.py / test_views_contributors.py | run 画面、SPSA/SPRT 表示、tests_repo | 要 | R |
| test_http_*.py, test_kvstore.py, test_lru_cache.py, test_views_finished.py, test_views_machines.py, test_views_routes.py, test_views_actions.py, test_views_admin.py, test_views_helpers.py, test_app.py | 汎用 HTTP 層・KV・キャッシュ・画面骨格 | 混在 | K |
| worker/tests/test_worker.py | ダウンロード・ツールチェーン・fastchess ビルド・updater | 不要（ネットワーク・コンパイラ要） | R |

## 10. Phase 0 の実施状況

- 棚卸し（本書）: 完了
- README 差し替え: 完了（fishtest の説明は docs/0-README.md 以下に残置）
- **fishtest 側コードの無効化は未実施**。bookforge を独立した ASGI アプリ（`server/bookforge/`）として作り、fishtest のコードには手を入れていないため、無効化・削除は bookforge 側で置き換えが揃った時点で、MongoDB のある環境で既存テストを回しながら行う（設計書 §13「削除は動作確認後」）。
- 受け入れ「サーバーが MongoDB だけで起動し、残すテストが通る」: この作業環境では MongoDB を取得できず（ダウンロード先がネットワークポリシーで遮断）、**未確認**。MongoDB 不要のテスト（test_app, test_lru_cache, test_views_finished など）は通ることを確認済み。
