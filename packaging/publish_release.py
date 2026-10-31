"""把打包好的 zip 传到 GitHub Release。

为什么不用 gh CLI：这台机器上没装，而 git 的凭据管理器里已经有 OAuth token
（`repo` scope），直接调 REST API 就够了，不必为一次发布再装个工具。

token 只在内存里用，不落盘、不打印。网络走本机代理（GitHub 直连在国内基本不可用）。

用法：
    py -3 packaging/publish_release.py            # 用 dist/ 下当前版本的 zip
    py -3 packaging/publish_release.py --draft    # 先发草稿，检查后再手动发布
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

APP_NAME = "HuaweiSmartLife"
#: GitHub 直连在国内基本不通，默认走本机代理；换环境时设 RELEASE_PROXY 覆盖
PROXY = os.environ.get("RELEASE_PROXY", "http://127.0.0.1:7897")
API = "https://api.github.com"
UPLOAD_TIMEOUT = 900


def repo_slug() -> tuple[str, str]:
    """从 git remote 解析 owner/repo，避免写死。"""
    out = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    if not out:
        raise SystemExit("读不到 origin 地址")
    slug = out.rstrip("/").split("/")[-1]
    if slug.endswith(".git"):
        slug = slug[:-4]
    owner = out.rstrip("/").split("/")[-2].split(":")[-1]
    return owner, slug


def token() -> str:
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    values = dict(
        line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line
    )
    value = values.get("password", "")
    if not value:
        raise SystemExit("取不到 GitHub 凭据，请先 git push 一次或手动登录")
    return value


def _proxy_opener() -> urllib.request.OpenerDirector:
    handler = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    return urllib.request.build_opener(handler)


def api_request(
    url: str,
    auth: str,
    method: str = "GET",
    payload: dict | None = None,
    allow_404: bool = False,
) -> dict:
    data = None
    headers = {
        "Authorization": f"Bearer {auth}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "huawei-smartlife-release",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = _proxy_opener()
    try:
        with opener.open(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if allow_404 and error.code == 404:
            return {}
        detail = error.read().decode("utf-8", errors="replace")[:400]
        raise SystemExit(f"{method} {url} 失败：{error.code} {detail}") from error


def upload_asset(upload_url: str, auth: str, path: str) -> dict:
    """分块读文件上传；100 MB 塞进内存没必要。"""
    name = os.path.basename(path)
    size = os.path.getsize(path)
    url = f"{upload_url}?name={urllib.parse.quote(name)}"
    headers = {
        "Authorization": f"Bearer {auth}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/zip",
        "Content-Length": str(size),
        "User-Agent": "huawei-smartlife-release",
    }
    with open(path, "rb") as handle:
        request = urllib.request.Request(url, data=handle, headers=headers, method="POST")
        opener = _proxy_opener()
        print(f"上传中：{name}（{size / 1024 / 1024:.1f} MB）…", flush=True)
        try:
            with opener.open(request, timeout=UPLOAD_TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise SystemExit(f"上传失败：{error.code} {detail}") from error


def release_body(version: str) -> str:
    return f"""## 华为智慧生活 PC 控制台 v{version}

Windows 10/11 x64。解压即用，**不需要安装 Python**。

### 下载

- `{APP_NAME}-{version}-win64.zip`（约 100 MB）

解压后双击 `{APP_NAME}.exe`。

### 这个版本包含

- 华为账号登录（含二次验证、会话保持），设备列表与状态查询
- 开关 / 亮度 / 温度 / 模式等基本控制，只读诊断收进「显示高级信息」
- 托盘面板与桌面挂件：可拖动、锁定、收起；拖到屏幕边缘自动吸附并贴边隐藏
- 挂件标题栏相机按钮：一点看实时画面（**仅开放 RTSP 的型号**，如小豚当家室外 3K / 5 4K）
- 设置两列布局；开机自启动（登录后静默到托盘，不在任务管理器里显示成 Python）

### 注意

- **非官方客户端**：接口基于社区逆向，华为更新后可能失效；仅供个人学习研究
- 摄像头实时画面受型号限制，室内机与加密机型只能在手机 App 看（详见 README）
- 程序**未签名**，首次运行 Windows 可能提示「已保护你的电脑」，点「更多信息 → 仍要运行」
- 登录凭据明文保存在 `%USERPROFILE%\\.huawei_smartlife\\session.json`，仅本机使用
"""


def main() -> int:
    draft = "--draft" in sys.argv
    from huawei_smartlife.version import VERSION

    owner, repo = repo_slug()
    tag = f"v{VERSION}"
    zip_path = os.path.join(ROOT, "dist", f"{APP_NAME}-{VERSION}-win64.zip")
    if not os.path.exists(zip_path):
        print(f"没有发布包：{zip_path}\n先跑 py -3 packaging/build_release.py")
        return 1

    auth = token()
    print(f"仓库：{owner}/{repo}   标签：{tag}   草稿：{draft}")

    existing = api_request(
        f"{API}/repos/{owner}/{repo}/releases/tags/{tag}", auth, allow_404=True
    )
    if existing.get("id"):
        print(f"release 已存在：{existing.get('html_url')}")
        print("如需重新上传，请先在网页上删掉该 release 再跑一次")
        return 0

    release = api_request(
        f"{API}/repos/{owner}/{repo}/releases",
        auth,
        method="POST",
        payload={
            "tag_name": tag,
            "name": f"v{VERSION}",
            "body": release_body(VERSION),
            "draft": draft,
            "prerelease": False,
        },
    )
    print(f"已创建 release：{release.get('html_url')}")

    asset = upload_asset(release["upload_url"].split("{")[0], auth, zip_path)
    print(f"已上传资产：{asset.get('name')}（{asset.get('size', 0) / 1024 / 1024:.1f} MB）")
    print(f"下载地址：{asset.get('browser_download_url')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
