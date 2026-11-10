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
- **多路同屏**：托盘右键一次看最多 9 路，自动按 1/2/4/6/9 格排布，每路独立解码，
  一路断流不影响其它格；选中的组合会记住
- **摄像头声音**：解 RTSP 里的音轨（G.711 / AAC）。单台画面窗可静音；多路同屏同一时刻
  只让一路出声
- **音量可调**：喇叭左键静音、右键调音量（30%–300%，超过 100% 是软件增益），
  设置 →「摄像头」→「默认音量」定每次启动的初始音量
- **全屏**：单台画面与多路同屏都支持，双击画面或点标题栏按钮，`Esc` 退出
- **桌面磁贴**：每台设备一块独立小方窗（图标 + 名称 + 开关 + 关键读数），
  拖到桌面任意位置、各自记住坐标；与桌面挂件并存，托盘里各自开关。
  支持**自选设备**、**三档尺寸**（紧凑/标准/大）、**拖动吸附**（网格对齐 / 贴边吸附）
  和「显示状态文字」开关。
  支持**自选设备**、**三档尺寸**（紧凑/标准/大）、**拖动吸附**（网格对齐 / 贴边吸附）
  和「显示状态文字」开关
- **桌面磁贴**：每台设备一块独立小方窗（图标 + 名称 + 开关 + 关键读数），
  拖到桌面任意位置、各自记住坐标；与桌面挂件并存，托盘里各自开关
- 设置两列布局；开机自启动（登录后静默到托盘，不在任务管理器里显示成 Python）
- **设置里可直接检查更新并一键升级**：下载新版本、自动替换、重启

### 本版修复

- 多路同屏删掉格子后排版错乱（旧的行 stretch 仍占着高度，画面被挤成一条窄带）
- 多路同屏每次增删都重连所有摄像头（改成只动变动的那一格）
- 多路同屏在全屏状态下删格子会丢标题栏，导致「加不了也关不掉」
- 桌面挂件开机后不自动出现（设置被测试脚本误写）
- **全屏退出后画面错位、标题栏消失**：这两个窗口是半透明 + 阴影特效，
  从全屏缩回时 Qt 只重画变化的局部区域，屏幕上留着全屏那一版的像素
  （几何其实是对的）。现在缩回后会补一次整窗重绘，单台画面窗同样修好
- **摄像头声音卡顿（一顿一顿）**：这路音频的帧是成批到达的（P99 间隔 130ms、
  最大 215ms），原来解一块写一块，两批之间就断音。改成声卡回调取数据 + 250ms
  预缓冲，实测 30 秒连续播放零欠载
- **连上实时画面后整个界面点不动**：音频启动里写错了一个属性名，首帧到达就抛
  异常，被全局异常钩子弹成模态「程序出错」框（框还压在画面窗后面）。
  已改正，并让音频启动整段兜住异常——声音坏了最多没声音，不影响画面
- **磁贴电源键点不动 / 网格对齐把两块粘成一片 / 磁贴不该置顶**：内容区误用了
  「鼠标事件穿透」，连里面的按钮一起失效；网格步长从死的 16 像素改成按磁贴尺寸，
  块间恒定 12px；去掉 WindowStaysOnTopHint，磁贴现在待在桌面上、会被其他窗口盖住
- **磁贴电源键点不动 / 网格对齐把两块粘成一片 / 磁贴不该置顶**：内容区误用了
  「鼠标事件穿透」，连里面的按钮一起失效；网格步长从死的 16 像素改成按磁贴尺寸，
  块间恒定 12px；去掉 WindowStaysOnTopHint，磁贴现在待在桌面上、会被其他窗口盖住
- **连上实时画面后整个界面点不动**：音频启动里写错了一个属性名，首帧到达就抛
  异常，被全局异常钩子弹成模态「程序出错」框（框还压在画面窗后面）。
  已改正，并让音频启动整段兜住异常——声音坏了最多没声音，不影响画面

### 更新方式

设置 →「更新」→ 检查更新 → 下载并更新 → 重启并安装。
程序会自己关闭、替换文件、再启动，不需要手动解压覆盖。

### 注意

- **非官方客户端**：接口基于社区逆向，华为更新后可能失效；仅供个人学习研究
- 摄像头实时画面受型号限制，室内机与加密机型只能在手机 App 看（详见 README）
- 程序**未签名**，首次运行 Windows 可能提示「已保护你的电脑」，点「更多信息 → 仍要运行」
- **开着 Smart App Control 的 Windows 会直接拦住本程序的 exe**（未签名，报
  「应用程序控制策略已阻止此文件」，错误码 0x11C7）。遇到这种情况要么改用源码运行，
  要么在「Windows 安全中心 → 应用和浏览器控制 → Smart App Control」里关掉它
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
