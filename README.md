# AGY-monitor · Antigravity 全中文实时运行与任务监控台 (v2.2.0)

专为 **Google Antigravity (AGY)** 打造的本地轻量级全中文实时运行监控面板（`http://127.0.0.1:8765`），随客户端启动自动拉起、零上下文 Token 消耗、零第三方依赖。

---

## 核心特性

- **侧边栏会话精准同步**：只读解析官方 `conversation_summaries.db`，100% 同步侧边栏会话名称与活跃状态，支持一键跟随最新活跃对话或手动切换历史会话。
- **后台长任务高精度 ETA 引擎**：
  - 自动识别 `tqdm` 进度条、`[Step k/N]` 阶段日志、显式步数计数器与 `schedule` 定时器；
  - 自动穿透命令行重定向的外部日志文件（`*.stdout.txt` / `*.log`）与 PowerShell `foreach` 多轮种子循环；
  - 支持**同目录已完成日志实测曲线动态倍率校准**与**带时间戳增量步速（`Δ步/Δt`）**，消除非线性阶段与断点续传造成的预估失真；
  - 自动识别服务重启中断与定时器提前唤醒，杜绝历史僵尸“运行中”任务。
- **子智能体 (Subagents) 精确计时**：实时追踪 `browser` / `research` 等子智能体的真实活跃状态、步数、上下文与净执行耗时。
- **上下文窗口 & 30 MB 载荷红线预警**：
  - 精确统计当前上下文窗口 Token（支持 `200K` / `256K 自动压缩线` / `1M` 参考切换）与 KV 缓存命中率；
  - 结合 `CHECKPOINT` 自动压缩边界与全量历史内联图片 Base64 体积，真实预警云端网关 `30 MB` 单次请求载荷上限。
- **Token 明细与生成速率趋势**：实时绘制上下文演化与各步生成速度（`tok/s`）双联 SVG 面积图，以及最新置顶的步骤耗时时间轴。
- **零依赖 · 零 Token 污染 · 纯本地只读**：100% Python 3 标准库实现，无需 `pip install`；通过空工具列表 MCP 守护通道托管生命周期，向大模型注入 `0` Token。

---

## 快速安装（三选一）

### 方式一：下载 ZIP 三秒一键安装（推荐 Windows / macOS / Linux）

1. 点击本仓库右上角 **`Code -> Download ZIP`**（或从右侧 **Releases** 下载 `AGY-monitor-v2.2.0.zip`）并完整解压；
2. **Windows**：双击运行 **`一键安装到AGY(Windows).bat`**（自动识别系统 Python / `py` / Anaconda / Miniconda，写入全局插件目录 `%USERPROFILE%\.gemini\config\plugins\ag-monitor` 并立即弹出监控页面）；
3. **macOS / Linux**：在解压目录终端执行：
   ```bash
   bash install_mac_linux.sh
   ```
4. 安装完成后，每次启动 Antigravity 都会在后台自动同步拉起服务，直接浏览器打开 **http://127.0.0.1:8765**（建议加入书签）即可查看！

### 方式二：Git Clone 到全局插件目录

```bash
git clone https://github.com/onluuqs/AGY-monitor.git ~/.gemini/config/plugins/ag-monitor
```
*(Windows PowerShell 可执行：`git clone https://github.com/onluuqs/AGY-monitor.git "$env:USERPROFILE\.gemini\config\plugins\ag-monitor"`)*

### 方式三：仅在单个项目工作区启用

将本仓库克隆或复制到 `<项目根目录>/.agents/plugins/ag-monitor` 即可。

---

## 管理与卸载

- **查看或开关插件**：在 Antigravity 左侧栏进入 `Customizations -> Plugins`，点击 **「AGY 实时运行监控台」** 即可随时开启或关闭（实时生效，无需重启）。
- **彻底卸载**：直接删除 `~/.gemini/config/plugins/ag-monitor` 目录即可，无任何注册表或系统残留。
