### bookforge（仮称）— 分散定石生成基盤

[fishtest](https://github.com/official-stockfish/fishtest) のフォークです。「対局で検定する基盤」を
「局面を探索して定石 DAG を育てる基盤」に作り替えています。

- コーディネータ（サーバー）が局面の展開・優先度・終了判定・値の伝播をすべて持ち、
  ワーカーは渡された局面を USI エンジンで探索して MultiPV を返すだけです。
- 局面 DAG は定石ブックごとに 1 つの SQLite ファイル（`books/<book_id>.db`）、
  run / task / worker は MongoDB に置きます。
- NNUE（cp）と DL（勝率）の評価値は別々に保持・伝播し、互いに換算しません。

| 文書 | 内容 |
|---|---|
| [docs/bookforge/design.md](docs/bookforge/design.md) | 設計書（決定事項・データ構造・API・フェーズと受け入れ条件） |
| [docs/bookforge/phase0-inventory.md](docs/bookforge/phase0-inventory.md) | fishtest の棚卸し（残す / 置き換える / 捨てる） |
| [docs/bookforge/phase1-notes.md](docs/bookforge/phase1-notes.md) | Phase 1 の実装メモ・設計書との差分・開発手順 |
| [docs/0-README.md](docs/0-README.md) 以下 | フォーク元 fishtest のドキュメント（置き換えが済むまで参照用に残置） |

#### 進捗

| フェーズ | 状態 |
|---|---|
| Phase 0: フォークと棚卸し | 棚卸し済み。fishtest 側コードの無効化は未着手（phase0-inventory.md §10） |
| Phase 1: サーバー MVP | `server/bookforge/` に実装。ダミーワーカーで run 完走・伝播の手計算テストあり |
| Phase 2 以降 | 未着手 |

#### すぐ試す

```bash
cd server
uv sync --group test
uv run python -m unittest discover -s tests -p "test_bookforge_*.py"
```

#### ライセンス

フォーク元に LICENSE ファイルが無いため、本リポジトリにもまだライセンスを付けていません。
詳しくは [phase0-inventory.md §0](docs/bookforge/phase0-inventory.md) を参照してください。
