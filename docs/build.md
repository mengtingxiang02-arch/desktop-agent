# 打包说明

把 Python 程序变成别人双击就能用的安装包：macOS 是 `.dmg`，Windows 是 `.exe`。用户不需要安装 Python，也不需要装任何依赖。

## 产物

| 平台 | 文件 | 在哪里打包 | 怎么打包 |
|---|---|---|---|
| macOS（Apple 芯片） | `DesktopAgent-<版本>-mac-arm64.dmg`（约 58 MB，里面的 `.app` 约 124 MB） | 自己的 Mac | PyInstaller 生成 `.app`，`hdiutil` 做成 `.dmg` |
| Windows 10 / 11（64 位） | `DesktopAgent-<版本>-windows.exe`（单个文件，约 63 MB） | GitHub Actions 的 Windows 云电脑 | PyInstaller `--onefile` |

下载地址：[Releases](https://github.com/mengtingxiang02-arch/desktop-agent/releases)。

## 用的工具：PyInstaller

PyInstaller 会找出 `gui.py` 用到的所有模块（PySide6、openai、mcp……），连同 Python 解释器本身一起装进安装包。

它**不能跨平台打包**：在 Mac 上只能打出 Mac 的包，Windows 的包必须在 Windows 上打。所以 Windows 版交给 GitHub Actions，在 GitHub 提供的 Windows 云电脑上打包。

依赖版本都钉死在 `requirements.txt` 里（包括 `pyinstaller==6.22.3`），本机打包和云端打包装到的是同一套依赖。

## macOS：打包 `.app` 和 `.dmg`

**1. 准备环境**（只需要做一次）

```bash
cd ~/Downloads/desktop-agent
uv venv --python 3.13
uv pip install -r requirements.txt
```

**一条命令完成下面的第 2、3 步**（先跑测试，任何一步出错就停下）：

```bash
bash scripts/package_mac.sh 1.1.0
```

脚本里做的事情如下。

**2. 生成 `.app`**

```bash
.venv/bin/pyinstaller --noconfirm "Desktop Agent.spec"
```

产物在 `dist/Desktop Agent.app`。`Desktop Agent.spec` 是 PyInstaller 的配置文件，几个关键设置：

- `console=False`：双击打开时不弹出终端窗口；
- `COLLECT`：文件夹模式（onedir），所有文件平铺在 `.app` 里，启动时不用先解压，打开得快；
- `BUNDLE`：把文件夹包装成 macOS 认识的 `.app`。

**3. 做成 `.dmg`**

```bash
rm -rf dist/dmg && mkdir dist/dmg
cp -R "dist/Desktop Agent.app" dist/dmg/
ln -s /Applications dist/dmg/Applications        # 打开 dmg 后可以直接拖进"应用程序"
hdiutil create -volname "Desktop Agent" -srcfolder dist/dmg -ov -format UDZO \
    dist/DesktopAgent-<版本>-mac-arm64.dmg
```

`UDZO` 是压缩格式，124 MB 的 `.app` 压缩后约 58 MB。

**4. 自己先试一遍**：双击 `.dmg`，把 Desktop Agent 拖进"应用程序"，再打开。程序没有签名，macOS 15 以后右键"打开"已经不能放行，要到 **系统设置 → 隐私与安全性**，在下面找到 Desktop Agent，点 **仍要打开**。

## Windows：用 GitHub Actions 打包 `.exe`

配置文件是 `.github/workflows/build-windows.yml`，步骤是：借一台 Windows 云电脑 → 下载代码 → 装 Python 3.13 → `pip install -r requirements.txt` → 打包 → 上传结果。

打包命令：

```
pyinstaller --onefile --windowed --name DesktopAgent gui.py
```

- `--onefile`：打成单个 `.exe`，发给别人方便。代价是每次启动都要先把内容解压到临时文件夹，打开会慢几秒；
- `--windowed`：不弹出黑色命令行窗口。

操作步骤：

1. 把代码推送到 GitHub；
2. 打开仓库的 **Actions** 页面，左边选 **Build Windows exe**，点 **Run workflow**；
3. 等几分钟变成绿色对勾，点进这次运行，在页面最下面的 **Artifacts** 下载 `DesktopAgent-windows`（一个 zip），解压得到 `DesktopAgent.exe`。注意要点**最上面那次**运行，下面的是以前的旧版本——我就下载错过一次；
4. 改名为 `DesktopAgent-<版本>-windows.exe`，上传到 Release。

第一次运行时 Windows 会弹出 SmartScreen 提示"已保护你的电脑"，点 **更多信息 → 仍要运行**。

## 打包后和源码运行的两点区别

**1. 配置文件放在哪**

源码运行时，`.env`、`mcp.json`、`playground` 都在项目文件夹里。打包以后不行：程序在只读的安装包里，写不进文件；API Key 也绝不能打包进安装包。

所以 `gui.py` 里用 `sys.frozen`（PyInstaller 打包后才有的标记）判断：打包后改用用户主目录下的 `DesktopAgent` 文件夹。第一次打开时弹窗让用户输入 API Key，保存到 `~/DesktopAgent/.env`，同时生成默认的 `mcp.json`。

**2. 找不到 `uvx`**

从 Finder 双击打开的程序拿不到终端里的 `PATH`，`mcp.json` 里写的 `uvx` 会"找不到命令"，MCP 服务器启动失败。`agent/mcp_client.py` 在 `PATH` 里找不到命令时，会再去 `~/.local/bin`、`/opt/homebrew/bin`、`/usr/local/bin` 找一遍。

如果电脑上根本没装 `uv`（比如新装的 Windows），MCP 服务器会启动失败，对话区显示一条提示，其它功能照常使用。

## 发布一个新版本

1. 改完代码，运行测试：`.venv/bin/python -m unittest`；
2. 按上面的步骤打包 Mac 和 Windows 两个安装包，各自试一遍；
3. 在 GitHub 上新建 Release，Tag 填版本号（如 `v1.1.0`），写清楚改了什么，上传两个安装包。

## 已知限制

- 安装包没有签名，第一次打开需要手动放行（苹果和微软的签名都要付费账号）。
- Mac 版只在 Apple 芯片上打包和测试过，Intel 芯片的 Mac 用不了。
- 安装包里的 Python 只给程序自己用。Agent 用 `bash` 运行 Python 程序时，用的是电脑上另外安装的 Python。
