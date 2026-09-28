"""一键打包: 前端构建 + 后端源码 + compose 部署文件 -> 一个可交付的部署目录/压缩包。

用法::

    uv run python -m scripts.package                 # 全流程(含 pnpm build)
    uv run python -m scripts.package --check-only    # 只跑前置校验与密钥审计
    uv run python -m scripts.package --skip-frontend # 复用现有 web/dist(刚打过一次)
    uv run python -m scripts.package --tar           # 产出 tar.gz 而非 zip
    uv run python -m scripts.package --strict-config # 在包内跑 docker compose config 验证

产物形态(按既定取舍: 只出部署目录, 不含镜像 tar):
``build/mxi-deploy-<版本>-<git短sha>-<时间戳>/`` + 同名 ``.zip``。目标机上
``docker compose ... up -d --build`` 自行构建镜像 —— Dockerfile 里 ``COPY web ./web``
正好把包内 ``web/dist`` 打进镜像, 前后端一体交付。

两条安全闸门(顺序执行, 任一不过直接非 0 退出且不产出包):
1. **源侧密钥审计**: 本机 ``.env`` / ``docker/.env`` 里出现明文密钥即中止 —— 说明密钥
   写错了位置(应在 docker/secrets/), 打包顺手把它带出机器是不可接受的。
2. **产物密钥扫描**: 对输出目录逐文件跑正则, 命中且不在白名单 -> 删除该文件并中止。
   黑名单在复制阶段就生效(``.env`` / ``docker/.env`` / ``docker/secrets/*.txt`` /
   上传件 / 模型权重 / node_modules 等根本不进包), 扫描是兜底而非唯一防线。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "build"

# ---------------------------------------------------------------- 打包内容清单

# 顶层整目录复制(src -> dst 同名); 会先过 EXCLUDE_DIRS / EXCLUDE_FILES。
INCLUDE_DIRS = (
    "app",
    "scripts",
    "docker",
    "web",  # 只有 dist 子目录不被排除, 见 EXCLUDE_DIRS
)

# 顶层单文件复制。
INCLUDE_FILES = (
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "langgraph.json",
    "README.md",
    "CONFIG_RULES.md",
    ".dockerignore",
    ".gitignore",
    ".env.example",
)

# 语料目录单独处理: 默认带上样例知识, --skip-knowledge 时跳过。
KNOWLEDGE_DIR = "data/knowledge"

# 整目录名(任意层级命中即剪掉)。node_modules / __pycache__ 这类会拖慢并污染产物;
# dist 不在这里 —— web/dist 是前端产物, 必须进包。
EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".git",
    ".qoder",
    ".trae",
    ".langgraph_api",
    ".idea",
    ".vscode",
    "logs",
    "reports",
    "build",
    "uploads",
    "tei_models",
    "msmarco",
    "pgdata",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

# 相对路径精确排除(黑名单优先于 INCLUDE_*); 真实配置与密钥永不进包。
EXCLUDE_PATHS = {
    ".env",
    ".env.local",
    "docker/.env",
    "data/.env",
}

# docker/secrets/ 目录里只允许使用说明进包(*.txt 是真实凭据)。
SECRETS_KEEP = {"docker/secrets/README.md"}

# ---------------------------------------------------------------- 密钥审计

# dotenv 里出现这些键的非空值 = 密钥写错了地方。
SECRET_KEYS = (
    "DEEPSEEK_API_KEY",
    "LANGSMITH_API_KEY",
    "PG_PASSWORD",
    "MONGO_PASSWORD",
    "NEO4J_PASSWORD",
    # 联网检索 provider 密钥(可选, 默认 ddgs 免密): 启用 Tavily/Serper 时同样只住 secrets。
    "TAVILY_API_KEY",
    "SERPER_API_KEY",
)

# 产物扫描规则: 名称 -> (正则, 是否高置信)。
# 高置信(私钥体/sk- 前缀/云 AK/DSN 内嵌口令)即使在注释行也算泄漏 —— 真密钥被写在
# "只是注释"里同样会随包外传; 低置信的"key: 长值"模式才允许注释行豁免。
SCAN_RULES: dict[str, tuple[re.Pattern[str], bool]] = {
    "openai 风格密钥": (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), True),
    "私钥文件体": (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), True),
    "DSN 内嵌口令": (re.compile(r"postgresql(?:\+\w+)?://[^:/\s@]+:[^@\s]+@"), True),
    "云厂商 AccessKey": (re.compile(r"\b(AKIA[0-9A-Z]{16}|LTAI[A-Za-z0-9]{12,})"), True),
    "赋值型口令/令牌": (
        re.compile(
            r"""(?i)\b(api[_-]?key|apikey|secret|password|passwd|token)\b\s*[:=]\s*['"]?([^\s'",;)]{12,})"""
        ),
        False,
    ),
}

# 命中值里的占位/示意特征: 模板与文档必须能安全使用这些键名。
_PLACEHOLDERISH = (
    "<",
    "your",
    "example",
    "placeholder",
    "changeme",
    "change_me",
    "replace",
    "xxx",
    "***",
    "dummy",
    "sample",
    "todo",
    # DSN/地址里的典型写意占位(user:pw@host), 不是真凭据
    "user:pw",
    "user:pass",
    ":pw@",
    ":pass@",
    "admin:admin",
)

# 低置信规则下, 这些符号说明命中的是代码表达式/模板而不是字面量密钥。
# 故意不含 . 与 + / : base64/JWT 类真密钥会带这些字符, 当成"代码引用"放过就是漏报。
_REF_CHARS = "(){}[]$%<>|&~^\\"
_REF_WORDS = {"password", "passwd", "secret", "token", "apikey", "api_key", "none", "null", "true", "false"}
# 全小写点号链: settings.pg_password / self._secret —— 变量引用不是字面量
_DOTTED_IDENT = re.compile(r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+$")


def _looks_like_reference(value: str) -> bool:
    """命中值是否像代码表达式/变量引用(而非真密钥字面量)。

    真密钥几乎不会带括号/插值符号, 也不会写成全小写点号链; 不加这层过滤的话
    ``password = get_settings().pg_password`` 这类取密代码会被全部误判为泄漏,
    闸门一响就很快被人手动关掉 —— 误报多到一定量级就等于没有闸门。
    """
    if any(ch in value for ch in _REF_CHARS):
        return True
    stripped = value.lower().strip('"\'')
    return stripped in _REF_WORDS or bool(_DOTTED_IDENT.match(stripped))


def _is_text_file(path: Path, limit: int = 4_000_000) -> bool:
    if path.suffix.lower() in {
        ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".ttf",
        ".safetensors", ".bin", ".gz", ".zip", ".whl", ".so", ".dll", ".pyd",
    }:
        return False
    try:
        return path.stat().st_size <= limit
    except OSError:
        return False


def _audit_source_secrets() -> list[str]:
    """第一道闸门: 本机 dotenv 文件里不得有明文密钥。"""
    problems: list[str] = []
    for rel in (".env", ".env.local", "docker/.env"):
        path = BASE_DIR / rel
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip().upper()
            value = value.strip().strip('"').strip("'")
            if key in SECRET_KEYS and value:
                problems.append(
                    f"{rel}: {key} 有明文值 -> 请移到 docker/secrets/{key.lower()}.txt "
                    f"(见该目录 README.md), dotenv 里留空"
                )
    return problems


def _scan_staged_file(path: Path, staging: Path) -> list[str]:
    """第二道闸门: 单个已入包文件的可疑内容(返回规则名与位置, 不回显命中值)。"""
    findings: list[str] = []
    if not _is_text_file(path):
        return findings
    rel = path.relative_to(staging).as_posix()
    if rel in SECRETS_KEEP:
        return findings
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return findings
    is_template = path.suffix == ".example" or ".env.example" in rel
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        for name, (pattern, high_confidence) in SCAN_RULES.items():
            match = pattern.search(line)
            if not match:
                continue
            value = (match.groups()[-1] if match.groups() else match.group(0)) or ""
            low = value.lower()
            if any(marker in low for marker in _PLACEHOLDERISH):
                continue
            if not high_confidence:
                # 纯数字长串(时间戳/端口/金额)不是令牌; 代码引用与注释里的配置说明也不当泄漏
                if len(value) >= 12 and not re.search(r"[A-Za-z]", value):
                    continue
                if _looks_like_reference(value):
                    continue
                if stripped.startswith("#") and not is_template:
                    continue
            findings.append(f"{rel}:{lineno} 命中规则[{name}]")
    return findings


def _excluded_dir(name: str) -> bool:
    return name in EXCLUDE_DIRS or name.endswith(".egg-info")


def _copy_tree(src: Path, dst: Path, rel_root: Path) -> int:
    """按黑名单递归复制目录, 返回复制的文件数。

    ``rel_root`` 是"本目录在包内的相对路径": 黑名单里既有按目录名判的
    (node_modules / __pycache__), 也有按完整路径判的(docker/.env), 只看文件名会漏。
    """
    if not src.is_dir():
        return 0
    count = 0
    for entry in sorted(src.iterdir()):
        rel = (rel_root / entry.name).as_posix()
        if rel in EXCLUDE_PATHS:
            continue
        if entry.is_dir():
            if _excluded_dir(entry.name):
                continue
            if rel == "docker/secrets":
                # 只带 README.md, 真实凭据留在本机
                target_dir = dst / entry.name
                target_dir.mkdir(parents=True, exist_ok=True)
                readme = entry / "README.md"
                if readme.is_file():
                    shutil.copy2(readme, target_dir / "README.md")
                    count += 1
                continue
            count += _copy_tree(entry, dst / entry.name, Path(rel))
            continue
        if entry.suffix == ".pyc":
            continue
        target = dst / entry.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, target)
        count += 1
    return count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() or "nogit"
    except Exception:  # noqa: BLE001 - 没装 git 也能打包
        return "nogit"


def _project_version() -> str:
    """从 pyproject.toml 取版本(与代码同源, 不额外维护版本号)。"""
    try:
        import tomllib

        with (BASE_DIR / "pyproject.toml").open("rb") as fh:
            data = tomllib.load(fh)
        return str(data.get("project", {}).get("version", "0.0.0"))
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _resolve_exe(name: str) -> str | None:
    return shutil.which(name) or shutil.which(name + ".cmd") or shutil.which(name + ".bat")


def _run(cmd: list[str], cwd: Path) -> int:
    print("$ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=str(cwd))


def preflight(require_frontend: bool) -> list[str]:
    """返回阻断性缺失项的说明列表(空表示可以继续)。"""
    problems: list[str] = []
    if sys.version_info < (3, 11):
        problems.append(f"需要 Python >= 3.11, 当前 {sys.version.split()[0]}")
    for rel in (".env.example", "docker/.env.example", "docker/docker-compose.yml", "docker/Dockerfile"):
        if not (BASE_DIR / rel).is_file():
            problems.append(f"缺少 {rel}(模板/部署文件不能少)")
    if not (BASE_DIR / "app").is_dir():
        problems.append("缺少 app/ 目录(不在仓库根运行?)")
    if require_frontend:
        if not _resolve_exe("pnpm"):
            problems.append("未找到 pnpm: 前端构建需要它(或先手工 build 出 web/dist 再带 --skip-frontend)")
        if not (BASE_DIR / "web-ui" / "package.json").is_file():
            problems.append("缺少 web-ui/package.json")
    elif not (BASE_DIR / "web" / "dist" / "index.html").is_file():
        problems.append("--skip-frontend 但 web/dist/index.html 不存在: 先跑一次不带该参数的打包")
    return problems


def build_frontend(skip_install: bool) -> None:
    web_dir = BASE_DIR / "web-ui"
    pnpm = _resolve_exe("pnpm")
    if not pnpm:
        raise SystemExit("未找到 pnpm")
    if not skip_install:
        if _run([pnpm, "install", "--frozen-lockfile"], web_dir) != 0:
            raise SystemExit("pnpm install --frozen-lockfile 失败")
    if _run([pnpm, "build"], web_dir) != 0:
        raise SystemExit("pnpm build 失败")
    index = BASE_DIR / "web" / "dist" / "index.html"
    if not index.is_file():
        raise SystemExit(f"构建结束但 {index} 不存在, 检查 vite build.outDir 配置")


def assemble(staging: Path, skip_knowledge: bool) -> int:
    copied = 0
    for name in INCLUDE_DIRS:
        src = BASE_DIR / name
        if name == "web":
            # 只带构建产物, 不带 web-ui 源码(目标机不需要 node 工具链)
            dist = src / "dist"
            if dist.is_dir():
                copied += _copy_tree(dist, staging / "web" / "dist", Path("web/dist"))
            continue
        if name == "docker":
            copied += _copy_docker(staging)
            continue
        copied += _copy_tree(src, staging / name, Path(name))
    # 顶层单文件(含 .env.example 模板; docker/.env.example 由 _copy_docker 带)
    for file_name in INCLUDE_FILES:
        src = BASE_DIR / file_name
        if src.is_file():
            dst = staging / file_name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    if not skip_knowledge and (BASE_DIR / KNOWLEDGE_DIR).is_dir():
        copied += _copy_tree(BASE_DIR / KNOWLEDGE_DIR, staging / KNOWLEDGE_DIR, Path("data/knowledge"))
    return copied


def _copy_docker(staging: Path) -> int:
    """docker/ 目录: 部署文件 + init SQL + mineru 构建文件 + 模板, 但不带真实凭据与 .env。"""
    src = BASE_DIR / "docker"
    dst = staging / "docker"
    count = 0
    for entry in sorted(src.rglob("*")):
        rel = entry.relative_to(src).as_posix()
        if not entry.is_file():
            continue
        if rel == ".env":
            continue  # 容器侧真实配置留本机
        if rel.startswith("secrets/") and rel != "secrets/README.md":
            continue  # 凭据留本机, 只带 README
        if _excluded_dir(entry.parent.name):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, target)
        count += 1
    return count


def write_deploy_doc(staging: Path, name: str) -> None:
    content = f"""# 部署步骤 ({name})

包内含后端源码、前端构建产物(`web/dist`)与 compose 编排, 目标机自行构建镜像。

## 0. 前置

- Docker Desktop(或 docker engine) + docker compose v2
- 宿主机 Ollama 已起并 `ollama pull bge-m3`(embedding 唯一不走 docker 的依赖)
- TEI 重排权重预下载(内网不可达时 TEI 无法自下载):

```powershell
$env:HF_ENDPOINT='https://hf-mirror.com'; $env:HF_HUB_DISABLE_XET='1'
uvx --from "huggingface_hub<1" hf download BAAI/bge-reranker-v2-m3 `
  config.json model.safetensors sentencepiece.bpe.model `
  special_tokens_map.json tokenizer.json tokenizer_config.json `
  --local-dir data/tei_models/BAAI/bge-reranker-v2-m3
```

## 1. 配置与密钥(密钥不进包, 必须现场创建)

```powershell
Copy-Item .env.example .env                      # 宿主直跑网关时才需要; 纯容器部署可跳过
Copy-Item docker/.env.example docker/.env        # compose 插值 + 容器侧配置注入源
New-Item -ItemType Directory -Force docker/secrets | Out-Null
# 口令自己生成, 不要复用示例值; 详见 docker/secrets/README.md
Set-Content -NoNewline docker/secrets/pg_password.txt "换成你的PG口令"
Set-Content -NoNewline docker/secrets/deepseek_api_key.txt "换成你的DeepSeekKey"
```

> 密钥只放 `docker/secrets/*.txt`: compose 以 secret 文件挂载到 `/run/secrets/<name>`,
> 应用代码从这里读。**不要**把密钥写进 `.env`/`docker/.env`(会被注入容器环境变量,
> 也会随配置备份外泄), 打包脚本对此有硬性拦截。

## 2. 起服务

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

若宿主机 Windows 端口排除段与默认发布端口(18000/18001/18002/17474)冲突, 在
`docker/.env` 里改 `*_HOST_PORT` 即可, 容器内端口不受影响。

## 3. 初始化数据

```bash
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.init_db
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.ingest_knowledge --dir /data/knowledge
docker compose -f docker/docker-compose.yml exec assistant python -m scripts.seed_business_data --force
```

## 4. 访问与自检

- 页面: http://localhost:18000 (前端已打进镜像, 由网关直接托管)
- 健康: http://localhost:18000/health
- 对接自检(宿主跑): `uv run python -m scripts.dev_services check`

## 5. 升级/回滚

- 升级: 换包后 `docker compose -f docker/docker-compose.yml up -d --build`, 数据在卷里不丢
- 回滚: 保留旧包目录, 重新 `up -d --build` 即可; PG/ES/Mongo/Neo4j 卷不动
- 注意 `NORMALIZER_VERSION` 与 `normalize_text()` 行为同升降(见 CONFIG_RULES.md 第 4 条)
"""
    (staging / "DEPLOY.md").write_text(content, encoding="utf-8")


def scan_staged(staging: Path) -> list[str]:
    findings: list[str] = []
    scanned = 0
    for path in sorted(staging.rglob("*")):
        if not path.is_file():
            continue
        # 必须用"包内相对路径"判排除: path 是绝对路径, parts 里带着仓根的 build/, 
        # 直接拿 parts 比对会让每一条文件都被当成排除项 —— 扫描变成空转。
        if any(part in EXCLUDE_DIRS for part in path.relative_to(staging).parts):
            continue
        if _is_text_file(path):
            scanned += 1
        findings += _scan_staged_file(path, staging)
    print(f"产物密钥扫描: 已扫 {scanned} 个文本文件, 命中 {len(findings)} 处")
    return findings


def make_archive(staging: Path, out_root: Path, use_tar: bool) -> Path:
    name = staging.name
    if use_tar:
        target = out_root / f"{name}.tar.gz"
        with tarfile.open(target, "w:gz") as tf:
            tf.add(staging, arcname=name)
    else:
        target = out_root / f"{name}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    zf.write(path, arcname=Path(name) / path.relative_to(staging))
    return target


def write_manifest(staging: Path, meta: dict) -> dict:
    entries = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file():
            continue
        entries.append(
            {
                "path": path.relative_to(staging).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    manifest = {**meta, "files": entries}
    (staging / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def validate_compose(staging: Path) -> None:
    """包内跑 ``docker compose config``: 验证目标机拿到包能渲染出部署文件。

    compose 里应用服务声明了 ``env_file: [.env]``, 而包里不带真实 docker/.env(配置属于
    现场创建), 所以临时用模板复制一份来渲染, 完事立即删除 —— 不能让它留在产物里。
    """
    docker_bin = _resolve_exe("docker")
    if not docker_bin:
        print("跳过 compose config 校验: 本机没有 docker")
        return
    env_src = staging / "docker" / ".env.example"
    env_dst = staging / "docker" / ".env"
    shutil.copy2(env_src, env_dst)
    try:
        rc = subprocess.call(
            [docker_bin, "compose", "-f", "docker/docker-compose.yml", "config", "--quiet"],
            cwd=str(staging),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
    finally:
        env_dst.unlink(missing_ok=True)
    if rc != 0:
        raise SystemExit("docker compose config 校验失败: 部署文件在目标机上渲染不出来")
    print("docker compose config 校验通过")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一键打包前后端为部署目录/压缩包")
    parser.add_argument("--skip-frontend", action="store_true", help="复用现有 web/dist")
    parser.add_argument("--skip-install", action="store_true", help="跳过 pnpm install")
    parser.add_argument("--skip-knowledge", action="store_true", help="不带 data/knowledge 样例语料")
    parser.add_argument("--tar", action="store_true", help="产出 tar.gz(默认 zip)")
    parser.add_argument("--check-only", action="store_true", help="只跑校验与密钥审计")
    parser.add_argument("--strict-config", action="store_true", help="包内跑 docker compose config 验证")
    args = parser.parse_args(argv)

    print(f"项目根: {BASE_DIR}")

    problems = preflight(require_frontend=not args.skip_frontend)
    if problems:
        print("前置校验未通过:", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 2

    secret_problems = _audit_source_secrets()
    if secret_problems:
        print("源侧密钥审计未通过:", file=sys.stderr)
        for item in secret_problems:
            print(f"  - {item}", file=sys.stderr)
        print("  打包会中止: 密钥只应存在于 docker/secrets/*.txt。", file=sys.stderr)
        return 2
    print("前置校验与密钥审计: 通过")

    if args.check_only:
        return 0

    if not args.skip_frontend:
        print("== 构建前端 ==")
        build_frontend(skip_install=args.skip_install)

    version = _project_version()
    sha = _git_sha()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"mxi-deploy-{version}-{sha}-{stamp}"
    out_root = OUT_DIR
    out_root.mkdir(parents=True, exist_ok=True)
    staging = out_root / name
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    print(f"== 组装部署目录 {staging.relative_to(BASE_DIR)} ==")
    copied = assemble(staging, skip_knowledge=args.skip_knowledge)
    write_deploy_doc(staging, name)

    findings = scan_staged(staging)
    if findings:
        print("产物密钥扫描未通过:", file=sys.stderr)
        for item in findings[:20]:
            print(f"  - {item}", file=sys.stderr)
        print(f"  (共 {len(findings)} 处; 已中止, 未产出压缩包)", file=sys.stderr)
        return 3

    manifest = write_manifest(
        staging,
        {
            "name": name,
            "version": version,
            "git_sha": sha,
            "built_at": datetime.now().isoformat(timespec="seconds"),
        },
    )

    if args.strict_config:
        validate_compose(staging)

    archive = make_archive(staging, out_root, use_tar=args.tar)
    size_mb = archive.stat().st_size / 1024 / 1024
    print("== 产物 ==")
    print(f"目录: {staging}")
    print(f"压缩包: {archive}  ({size_mb:.1f} MB, sha256={_sha256(archive)[:16]}…)")
    print(f"文件数: {len(manifest['files'])}")
    print("\n目标机操作见包内 DEPLOY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
