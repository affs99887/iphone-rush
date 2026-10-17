# iPhone Duo 预约助手

Windows 桌面项目，使用 PySide6 和 Playwright，包含门店自取、送货到家两路操作流程、状态面板与离线回归测试。

公开源码中的姓名、手机号、邮箱、详细地址和订单号均为示例或脱敏值。运行前请在设置中填写自己的联系方式，并核对预约时间与购买配置。已有的本地加密设置仍保存在 `data/` 中。

## 本地运行

在 Windows 10/11 x64 上安装 Python，然后在项目目录的 PowerShell 中执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD 'browser-runtime'
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe app.py
```

具体操作见 [使用说明](使用说明.txt)。

## 离线测试

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
```

测试使用本地页面记录和模拟对象，不执行真实官网下单。`tests/fixtures/` 中的页面记录保留结构，个人资料使用示例值替换；有清单的目录已更新文件哈希。

## 构建 EXE

安装上述依赖和 Chromium 后执行：

```powershell
.\build.ps1 -PythonExecutable .\.venv\Scripts\python.exe
```

产物写入 `release/`。构建脚本也可通过 `-PythonExecutable` 指定其他 Python 解释器。

## 本地文件

`data/`、`.private/`、个人信息 TXT、浏览器运行时、依赖、缓存及构建产物已由 `.gitignore` 排除。个人设置和浏览器会话应保留在本机。
