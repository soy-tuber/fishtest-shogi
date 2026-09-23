"""vtjson schemas for run configurations (design doc §4) and worker API
requests (§7)."""

from vtjson import (
    cond,
    ge,
    gt,
    intersect,
    keys,
    lax,
    le,
    one_of,
    regex,
    set_name,
    size,
    union,
)

from bookforge.bookdb import BOOK_ID_RE
from bookforge.shogi import is_usi_move

uint = intersect(int, ge(0))
pint = intersect(int, gt(0))
number = union(int, float)
unit_interval = intersect(number, ge(0), le(1))
sha256 = regex(r"[0-9a-f]{64}", name="sha256")
url = regex(r"https?://\S+", name="url")
book_id = regex(BOOK_ID_RE.pattern.strip("^$"), name="book_id")
platform = regex(r"(windows|linux|macos)-[a-z0-9_]+-[a-z0-9_]+", name="platform")
worker_id = regex(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", name="uuid"
)
task_id = regex(r"[0-9a-f]{24}", name="task_id")
usi_move = set_name(is_usi_move, "usi_move")
short_str = intersect(str, size(0, 200))
message = intersect(str, size(0, 5000))

artifact = {"url": url, "sha256": sha256}
usi_options = {regex(r"[A-Za-z0-9_]{1,64}", name="usi_option"): union(str, int, bool)}

engine_nnue = {
    "kind": "nnue",
    "family?": short_str,
    "binaries": intersect({platform: artifact}, size(1, 32)),
    "eval": artifact,
    "usi_options?": usi_options,
}

# DL engines (TensorRT) are machine-specific: instead of a distributed
# binary the worker may run a local engine and declare its hash (§8.3).
engine_dl = intersect(
    {
        "kind": "dl",
        "family?": short_str,
        "binaries?": intersect({platform: artifact}, size(1, 32)),
        "local_engine?": bool,
        "eval": artifact,
        "usi_options?": usi_options,
    },
    union(keys("binaries"), lax({"local_engine": True})),
)

engine = cond(
    (lax({"kind": "nnue"}), engine_nnue),
    (lax({"kind": "dl"}), engine_dl),
)

search = {
    "budget": pint,
    "multipv": intersect(int, ge(1), le(32)),
    "mode": union("st", "smp"),
}

policy = {
    "self_side": union("sente", "gote"),
    "self_eval_diff": uint,
    "opp_eval_diff": uint,
    "self_winrate_diff?": unit_interval,
    "opp_winrate_diff?": unit_interval,
    "max_ply_from_root": intersect(int, ge(0), le(512)),
    "min_budget_for_expand": uint,
    "reeval_if_unstable_cp?": uint,
}

stop = intersect(
    {
        "max_nodes?": pint,
        "max_core_hours?": intersect(number, gt(0)),
        "until_frontier_empty?": bool,
    },
    size(1, 3),
)

target = {"ply_min?": uint, "ply_max?": uint}

weights = {
    union(
        "w_line", "w_close", "w_freq", "w_split", "w_unst", "tau_nnue", "tau_dl"
    ): number
}

_common = {
    "book_id": book_id,
    "stop": stop,
    "priority?": int,
    "throughput?": intersect(number, gt(0), le(1000)),
    "verify_ratio?": unit_interval,
    "weights?": weights,
}

expand_args = intersect(
    {
        "type": "expand",
        "engine": engine,
        "search": search,
        "policy": policy,
        **_common,
    },
    set_name(
        lambda a: a["search"]["budget"] >= a["policy"]["min_budget_for_expand"],
        "budget_reaches_min_budget_for_expand",
    ),
)

reeval_args = {
    "type": "reeval",
    "engine": engine,
    "search": search,
    "target?": target,
    **_common,
}

dual_args = {
    "type": "dual",
    "engines": {"nnue": engine_nnue, "dl": engine_dl},
    "search": {"nnue": search, "dl": search},
    "target?": target,
    **_common,
}

run_args = cond(
    (lax({"type": "expand"}), expand_args),
    (lax({"type": "reeval"}), reeval_args),
    (lax({"type": "dual"}), dual_args),
    (lax({"type": "sprt"}), set_name(lambda _: False, "sprt_runs_arrive_in_phase_6")),
)

book_meta = {
    "self_side": union("sente", "gote"),
    "description?": message,
}

# worker API

hw = {
    "os": union("windows", "linux", "macos"),
    "cpu?": short_str,
    "threads?": uint,
    "flags?": intersect([regex(r"[a-z0-9_]{1,32}", name="cpu_flag"), ...], size(0, 64)),
    "gpus?": intersect([short_str, ...], size(0, 16)),
    "cuda?": union(short_str, None),
    "tensorrt?": union(short_str, None),
    "arch?": regex(r"[a-z0-9_]{1,16}", name="arch"),
}

bench = {
    "nnue_nps_per_thread?": uint,
    "dl_playouts_per_sec?": uint,
}

capability = union("nnue", "dl")

request_version = {
    "worker_uuid": worker_id,
    "version": uint,
    "sri?": short_str,
    "capabilities": intersect([capability, ...], size(1, 2)),
    "hw": hw,
    "bench?": bench,
    "name?": regex(r"[A-Za-z0-9_.-]{1,64}", name="worker_name"),
}

request_task = {
    "worker_id": worker_id,
    "capability": capability,
    "slots": intersect(int, ge(1), le(1024)),
    "local_engine_sha256?": sha256,
}

cand = intersect(
    {
        "move": usi_move,
        "score_cp?": intersect(int, ge(-31999), le(31999)),
        "score_mate?": intersect(int, ge(-1000), le(1000)),
        "winrate?": unit_interval,
        "pv?": intersect(str, size(0, 4000)),
    },
    one_of("score_cp", "score_mate", "winrate"),
)

result = {
    "pos_id": pint,
    "engine_hash": sha256,
    "eval_hash": sha256,
    "budget": pint,
    "depth?": uint,
    "seldepth?": uint,
    "elapsed_ms?": uint,
    "multipv": intersect([cand, ...], size(1, 32)),
}

update_task = {
    "worker_id": worker_id,
    "task_id": task_id,
    "final": bool,
    "results": intersect([result, ...], size(0, 5000)),
}

beat = {"worker_id": worker_id, "task_id": task_id}

failed_task = {"worker_id": worker_id, "task_id": task_id, "message?": message}

worker_log = {
    "worker_id": worker_id,
    "message": message,
    "task_id?": task_id,
}
