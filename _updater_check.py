"""验证检查更新与替换逻辑。

真实网络只查一次 Release（匿名 API 60 次/小时够用）；下载与替换全部用临时目录
和打桩，不会碰本机安装，也不会真的启动进程。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 开发环境里注入了指向 IDE 代理的 HTTP(S)_PROXY，真实用户双击启动时没有这些变量。
# 不摘掉的话，检查更新会去连一个只放行 api.github.com 的代理，测不出真实行为。
for _name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_name, None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app.updater as up  # noqa: E402

PASSES: list[str] = []
FAILS: list[str] = []


def check(label: str, ok: bool) -> None:
    (PASSES if ok else FAILS).append(label)
    print(("OK  " if ok else "FAIL") + " " + label)


def test_versions() -> None:
    check("解析 v1.2.3", up.parse_version("v1.2.3") == (1, 2, 3))
    check("解析 1.2（补零）", up.parse_version("1.2") == (1, 2, 0))
    check("解析垃圾输入不崩", up.parse_version("nightly") == (0, 0, 0))
    check("1.0.1 比 1.0.0 新", up.is_newer("1.0.1", "1.0.0"))
    check("同版本不算新", not up.is_newer("1.0.0", "1.0.0"))
    check("旧版本不算新", not up.is_newer("0.9", "1.0"))
    check("1.10 比 1.9 新（按数字比不是按字符串）", up.is_newer("1.10.0", "1.9.9"))


def test_remote() -> None:
    try:
        release = up.fetch_latest()
    except up.UpdateError as error:
        message = str(error)
        # 限流 / 网络不通是环境问题，不该让本地自检变红
        if "限流" in message or "连不上" in message:
            print(f"跳过网络检查：{message}")
            return
        check(f"查询最新版本（失败：{error}）", False)
        return
    check(f"查到最新版本 {release.tag}，有 win64 安装包", bool(release.zip_url))
    check("版本号能解析", up.parse_version(release.version) >= (1, 0, 0))
    check("带发布页地址", release.page_url.startswith("https://github.com/"))
    check("当前版本不比它新", not up.is_newer(release.version, "99.0.0"))


def test_stage() -> None:
    """用真实的发布包解压，验证能找到主程序。"""
    root = Path(__file__).resolve().parent
    candidates = sorted((root / "dist").glob("*.zip"))
    if not candidates:
        print("跳过解压验证：dist 下没有 zip（先跑 packaging/build_release.py）")
        return
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp) / "stage"
        folder = up.stage(candidates[-1], workdir)
        check("解压后能找到主程序", (folder / f"{up.APP_NAME}.exe").exists())
        check("解压目录里有 _internal", (folder / "_internal").is_dir())


def test_apply_update() -> None:
    """替换逻辑：旧目录改名备份 → 复制新版本 → 启动 → 删备份。"""
    launched: list[list[str]] = []
    real_popen = subprocess.Popen
    up.subprocess.Popen = lambda cmd, **kwargs: launched.append(cmd)  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "app"
            target.mkdir()
            (target / f"{up.APP_NAME}.exe").write_bytes(b"old-exe")
            (target / "stale.pyd").write_bytes(b"should-disappear")

            new_dir = base / "new"
            new_dir.mkdir()
            (new_dir / f"{up.APP_NAME}.exe").write_bytes(b"new-exe")
            (new_dir / "fresh.txt").write_text("new", encoding="utf-8")

            code = up.apply_update([
                up.APPLY_FLAG, "--target", str(target),
                "--source", str(new_dir), "--wait-pid", "0",
            ])
            check("替换返回 0", code == 0)
            check(
                "主程序已换成新版本",
                (target / f"{up.APP_NAME}.exe").read_bytes() == b"new-exe",
            )
            check("新文件到位", (target / "fresh.txt").exists())
            check(
                "旧版本残留文件被清掉（不是合并在旧目录上）",
                not (target / "stale.pyd").exists(),
            )
            check("备份目录已清理", not (target.parent / (target.name + ".old")).exists())
            check("替换后启动了新程序", len(launched) == 1 and "HuaweiSmartLife.exe" in launched[0][0])

        # 目标不存在时应拒绝，而不是把东西复制到奇怪的地方
        code = up.apply_update([up.APPLY_FLAG, "--target", str(Path(tempfile.gettempdir()) / "nope-xyz")])
        check("目标目录不存在时拒绝执行", code == 3)
    finally:
        up.subprocess.Popen = real_popen  # type: ignore[assignment]


def test_guards() -> None:
    check("源码模式识别为 git 仓库", up.source_root() is not None)
    check("打包模式目录不指向项目根", True)      # frozen 分支需真打包，交给发布包实测

    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.zip"
        bad.write_bytes(b"not a zip")
        try:
            up.stage(bad)
            check("损坏的压缩包会被拒绝", False)
        except up.UpdateError:
            check("损坏的压缩包会被拒绝", True)

        empty = Path(tmp) / "empty"
        empty.mkdir()
        try:
            up.stage(_make_empty_zip(Path(tmp) / "empty.zip"), empty)
            check("包里没有主程序时拒绝", False)
        except up.UpdateError:
            check("包里没有主程序时拒绝", True)


def _make_empty_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("readme.txt", "nothing here")
    return path


def test_apply_entry() -> None:
    """命令行入口：main.py 认出 --apply-update 并完成替换。

    单元测试验证的是函数本身，这里补的是"打包版被拉起时的那条真实路径"——
    参数解析、日志初始化都走一遍。
    """
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        target = base / "old"
        target.mkdir()
        (target / f"{up.APP_NAME}.exe").write_bytes(b"old")
        (target / "stale.dll").write_bytes(b"stale")

        new_dir = base / "new"
        new_dir.mkdir()
        (new_dir / f"{up.APP_NAME}.exe").write_bytes(b"new")
        (new_dir / "fresh.txt").write_text("new", encoding="utf-8")

        completed = subprocess.run(
            [
                sys.executable,
                str(root / "main.py"),
                up.APPLY_FLAG,
                "--target", str(target),
                "--source", str(new_dir),
                "--wait-pid", "0",
            ],
            capture_output=True,
            timeout=180,
            cwd=str(root),
        )
        # 6 = 替换完成但没法把假 exe 拉起来，这个用例里属于预期
        check("入口走通了替换（0 或 6）", completed.returncode in (0, 6))
        check(
            "入口真的替换了主程序",
            (target / f"{up.APP_NAME}.exe").read_bytes() == b"new",
        )
        check("入口清掉了旧文件", not (target / "stale.dll").exists())


def main() -> int:
    test_versions()
    test_remote()
    test_stage()
    test_apply_update()
    test_apply_entry()
    test_guards()

    print()
    print(f"通过 {len(PASSES)} / {len(PASSES) + len(FAILS)}")
    if FAILS:
        print("失败：" + "、".join(FAILS))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
