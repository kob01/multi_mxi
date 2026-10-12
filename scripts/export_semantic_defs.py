"""导出语义层真值 YAML: ``--from-seed``(代码内置集) 或 ``--from-db``(库里当前治理过的口径)。

两条方向的差别很实在:
- ``--from-seed`` 是**第一次落 YAML** 的路径(不需要库, 也不需要容器), 产物是可 review 的初稿;
- ``--from-db`` 是把"有人在库里热修过的口径"回写成真值文件 —— 库里改了但没回写 YAML,
  下一次同步就会把它刷掉, 这个脚本就是防这件事的那一步。

Run:
    python -m scripts.export_semantic_defs --from-seed
    python -m scripts.export_semantic_defs --from-db [--dir semantic-defs]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.semantic import defs as semantic_defs  # noqa: E402
from app.semantic.defs import DefsBundle  # noqa: E402


def _from_db(directory: Path) -> DefsBundle:
    """从库里读当前语义层(含人工修订)并组织成真值结构。"""
    from sqlalchemy import text

    from app.db import sync as dbsync

    engine = dbsync.get_sync_engine()
    bundle = DefsBundle()
    with engine.connect() as conn:
        for row in conn.execute(text("SELECT * FROM sem_metrics ORDER BY metric_id")):
            data = dict(row._mapping)
            data.pop("embedding", None)
            data["compatible_dims"] = list(data.get("compatible_dims") or [])
            bundle.metrics.append({k: v for k, v in data.items() if k in semantic_defs.METRIC_KEYS})
        for row in conn.execute(text("SELECT * FROM sem_dimensions ORDER BY dim_id")):
            data = dict(row._mapping)
            data.pop("embedding", None)
            data["enum_values"] = list(data.get("enum_values") or [])
            data["hierarchy"] = list(data.get("hierarchy") or [])
            bundle.dimensions.append({k: v for k, v in data.items() if k in semantic_defs.DIM_KEYS})
        for row in conn.execute(text("SELECT * FROM sem_terms WHERE source <> 'badcase' "
                                     "ORDER BY term, term_type, target_id")):
            data = dict(row._mapping)
            data.pop("embedding", None)
            data.pop("term_id", None)
            data["weight"] = float(data.get("weight") or 1.0)
            bundle.terms.append({k: v for k, v in data.items() if k in semantic_defs.TERM_KEYS})
        for row in conn.execute(text("SELECT * FROM sem_dim_values ORDER BY dim_id, value")):
            data = dict(row._mapping)
            data.pop("embedding", None)
            data.pop("value_id", None)
            data["aliases"] = list(data.get("aliases") or [])
            bundle.dim_values.append({k: v for k, v in data.items() if k in semantic_defs.VALUE_KEYS})
        for row in conn.execute(text("SELECT * FROM sem_metric_dim_compat "
                                     "ORDER BY metric_id, dim_id")):
            data = dict(row._mapping)
            data.pop("version", None)
            data.pop("updated_at", None)
            bundle.compat.append({k: v for k, v in data.items() if k in semantic_defs.COMPAT_KEYS})
        for row in conn.execute(text("SELECT * FROM sem_time_semantics ORDER BY cal_type")):
            data = dict(row._mapping)
            data.pop("version", None)
            data.pop("updated_at", None)
            bundle.time_semantics.append(
                {k: v for k, v in data.items() if k in semantic_defs.TIME_KEYS}
            )
        for row in conn.execute(text("SELECT * FROM sem_row_policies ORDER BY policy_id")):
            data = dict(row._mapping)
            for drop in ("enabled", "version", "updated_at"):
                data.pop(drop, None)
            bundle.row_policies.append(
                {k: v for k, v in data.items() if k in semantic_defs.ROW_POLICY_KEYS}
            )
        for row in conn.execute(text("SELECT * FROM sem_col_policies ORDER BY role, table_name")):
            data = dict(row._mapping)
            for drop in ("enabled", "version", "updated_at"):
                data.pop(drop, None)
            data["denied_cols"] = list(data.get("denied_cols") or [])
            bundle.col_policies.append(
                {k: v for k, v in data.items() if k in semantic_defs.COL_POLICY_KEYS}
            )
    if not bundle.metrics:
        raise SystemExit("库里 sem_metrics 是空的: 先跑 scripts/seed_semantic_layer 或改用 --from-seed")
    return bundle


def _report(paths: list[Path], bundle: DefsBundle) -> None:
    counts = {
        "metrics": len(bundle.metrics), "dimensions": len(bundle.dimensions),
        "terms": len(bundle.terms), "dim_values": len(bundle.dim_values),
        "compat": len(bundle.compat), "time_semantics": len(bundle.time_semantics),
        "row_policies": len(bundle.row_policies), "col_policies": len(bundle.col_policies),
    }
    print("已写出真值文件:")
    for path in paths:
        print(f"  - {path.relative_to(ROOT)}")
    print("条目数:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    problems = semantic_defs.validate_bundle(bundle)
    if problems:
        print("校验问题(请修完再同步):")
        for item in problems[:20]:
            print(f"  ! {item}")
        raise SystemExit(2)
    print("校验通过: 引用与谓词模板都合法")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出智能问数语义层真值 YAML")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-seed", action="store_true", help="由代码内置种子生成(不碰库)")
    source.add_argument("--from-db", action="store_true", help="由当前库里的语义层导出(含人工修订)")
    parser.add_argument("--dir", default=str(ROOT / "semantic-defs"), help="输出目录")
    args = parser.parse_args(argv)

    directory = Path(args.dir)
    bundle = _from_db(directory) if args.from_db else semantic_defs.bundle_from_seed()
    paths = semantic_defs.dump_bundle(bundle, directory)
    _report(paths, bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
