"""用 Aider 的 RepoMap 生成仓库代码地图 (repo map)。

RepoMap 不是"文件树", 而是 aider 的三阶段产物:
  1. Tree-sitter 抽取每个文件的符号 (class/def/import) 及其引用关系;
  2. PageRank 式的图排序 + 按"符号数/文件行数比"裁剪, 挑出最有信息量的符号;
  3. 渲染成带目录树缩进的签名清单, 在 token 预算内塞满"能帮模型建立全局认知"的行。

aider 是外部工具, 不装进本项目依赖 (它的 litellm/tree-sitter 栈会和项目锁冲突),
所以用 uv 的一次性隔离环境跑:

    uv run --no-project --with aider-chat python scripts/gen_repo_map.py \
        --map-tokens 16384 -o REPO_MAP.md

`--no-project` 保证不读 pyproject/不装依赖, 只临时建一个隔离环境。

预算怎么挑: `--map-tokens` 是硬约束, aider 按信息量排序往里装。4096 只能覆盖三分之一
文件 (适合每次对话随请求注入的轻量 map); 16384 会把全部文件列进目录树, 并给出每个模块
的完整签名 (适合当入库文档、一次性给模型建立全局认知)。

地图里的路径统一用 `/` (aider 在 Windows 上会给出 `app\\rag\\...`), 符号缓存改成进程
内存, 不在仓库里留下 `.aider.tags.cache.v*`。

注意: `aider --show-repo-map` 也能出图, 但 rich 会按终端宽度折行且 Windows
GBK 控制台会因 `⋮` 字符抛 UnicodeEncodeError; 直接调库写 UTF-8 文件没有这两个坑。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from diskcache import Cache

from aider.io import InputOutput
from aider.models import Model
from aider.repomap import RepoMap
from aider.repo import GitRepo


def posix_rel_fname(self: RepoMap, fname: str) -> str:
    """Windows 上 aider 会用 os.sep 拼相对路径, 地图里就成了 `app\\rag\\retriever.py`。

    LLM 与 Unix 工具链都认 `/`, 所以统一改写成正斜杠 (只影响渲染, 不影响符号抽取
    与语言识别: filename_to_lang 只看扩展名)。
    """
    return os.path.relpath(fname, self.root).replace(os.sep, "/")


def memory_tags_cache(self: RepoMap) -> None:
    """把 aider 的符号缓存改成进程内存, 不在仓库里落 `.aider.tags.cache.v*`。

    必须这么做还有另一个原因: 缓存条目里存的是含 rel_fname 的 Tag 元组,
    磁盘缓存会把旧的路分隔符一直带回来, 让上面的 posix 改写失效。
    中小仓库全量重析只花几秒, 不值得为缓存多出一个脏目录。
    """
    self.TAGS_CACHE = Cache()


RepoMap.get_rel_fname = posix_rel_fname
RepoMap.load_tags_cache = memory_tags_cache


def collect_fnames(root: Path, io: InputOutput, all_files: bool) -> list[str]:
    """待建图的文件列表 (绝对路径)。

    默认走 git 视野 (= aider 自己的行为): 只取已跟踪文件, 天然尊重 .gitignore,
    因此 .venv/data/uploads/reports 这些不会污染地图。--all-files 才退化为裸遍历。
    """
    if all_files:
        return sorted(
            str(path)
            for path in root.rglob("*")
            if path.is_file()
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        )

    repo = GitRepo(io, fnames=[str(root)], git_dname=None)
    return sorted(str(repo.abs_root_path(fname)) for fname in repo.get_tracked_files())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=".", help="仓库根目录 (默认当前目录)")
    parser.add_argument(
        "--map-tokens", type=int, default=16384, help="地图的 token 预算, 0 表示关闭 (默认 16384)"
    )
    parser.add_argument(
        "--model", default="gpt-4o-mini", help="仅用于 token 计数的模型名, 不联网调用"
    )
    parser.add_argument(
        "--all-files", action="store_true", help="忽略 git 跟踪状态, 裸遍历目录"
    )
    parser.add_argument("-o", "--out", help="输出文件, 省略则写到 stdout")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    os.chdir(root)

    io = InputOutput(pretty=False, fancy_input=False, yes=True)
    fnames = collect_fnames(root, io, args.all_files)
    print(f"scope: {len(fnames)} files", file=sys.stderr)
    if not fnames:
        print("没有可建图的文件", file=sys.stderr)
        return 1

    # chat_files 传空 = "还没有任何文件进对话", 于是整仓文件都算 other_files,
    # 这是离线批量建图的正确形态 (地图会覆盖全仓而非排除若干文件)。
    repo_map = RepoMap(
        map_tokens=args.map_tokens,
        root=str(root),
        main_model=Model(args.model),
        io=io,
    )
    content = repo_map.get_repo_map([], set(fnames)) or ""
    print(
        f"repo map: {len(content)} chars, ~{repo_map.token_count(content)} tokens",
        file=sys.stderr,
    )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8", newline="\n")
        print(f"wrote {out}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(content.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
