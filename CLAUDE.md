# CLAUDE.md

This repository is a fork of fishtest being turned into **bookforge**, a
distributed shogi opening-book generator. The spec is
[docs/bookforge/design.md](docs/bookforge/design.md) (Japanese); decisions
and deviations are in [docs/bookforge/phase1-notes.md](docs/bookforge/phase1-notes.md),
the fishtest inventory in [docs/bookforge/phase0-inventory.md](docs/bookforge/phase0-inventory.md).

## Working rules (design doc §13)

- 1フェーズずつ進め、各フェーズの「受け入れ」を満たすテストを先に書く
- fishtest 由来のコードは、置き換える前に「何を呼んでいるか」を一覧化してから触る。削除は動作確認後
- 局面の同一性は常に「手数を除いた正規化SFEN」で扱う。手数をキーに混ぜない（`bookforge.shogi.normalize`）
- NNUE と DL の評価値を同じカラムに入れない。換算もしない（`bookforge.values`）
- ワーカーは判断しない。展開・優先度・終了判定はすべてサーバー側に置く
- 秘密情報（Access のクライアントシークレット等）はコード・テスト・ログに出さない

## Layout

- `server/bookforge/` — the new coordinator (Phase 1 MVP). Independent of the
  fishtest game-testing code; only `fishtest.scheduler.Scheduler` is reused.
- `server/fishtest/` — original fishtest server, untouched so far.
- `worker/` — original fishtest worker (to become the USI search worker in Phase 2).

## Commands

```bash
cd server
uv sync --group test
uv run python -m unittest discover -s tests -p "test_bookforge_*.py"   # no MongoDB needed (mongomock)
uv run python -m unittest discover -vb -s tests                         # full suite, needs mongod on localhost
uvx ruff@0.16.8 check . && uvx ruff@0.16.8 format --check .             # same ruff as pre-commit
BOOKFORGE_AUTH=stub uv run uvicorn bookforge.app:app --port 8000        # dev server (needs mongod)
uv run python -m bookforge.dummy_worker --url http://127.0.0.1:8000 --once
```

Server code targets Python 3.14; worker code must stay Python 3.8 compatible.
