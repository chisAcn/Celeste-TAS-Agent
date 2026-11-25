# DeepSeek 玩 Celeste

一个在《蔚蓝》实机中规划路线、执行按键并验证结果的实验项目。Python Agent 通过 Everest Mod 读取角色位置、速度、碰撞地形和房间出口。DeepSeek 或 Codex 可以提出多帧动作计划；本地执行器负责逐帧输入、记录轨迹和检查落点。有存档和策略表时，本地搜索也可以独立运行。

## 当前进度

截至 2026-11-25，项目已记录人机协作通过教学地图 **6b → 6c → 7 → 8** 的路线。近期工作集中在另一项能力：从固定起点出发，只给 **8 次尝试**，本地策略能否找到并复现一条短路线。整章自主通关仍是开放目标。

| 实机任务 | 当前结果 | 判定方法 |
| --- | --- | --- |
| 房间内导航 | **7/8** | 房间 `1` 和 `6a` 各 4 个目标；有效终点须留在原房间，距目标不超过 6 像素 |
| 跨房间出口 | **第 2 次成功** | 从 `6a` 进入房间 `6` 并通过回放核验，单独计分 |
| 项目测试 | **223 项通过** | 最近一次完整运行 `python -m pytest tests -q` |

在同一组导航任务上，固定 10 帧策略完成了 3/8；允许第二段变长并按后续动作评分保留分支后达到 6/8。最新改动只影响一个具体情形：末步所有候选都没有评分、角色站在地上且目标较高时，优先尝试朝目标跳跃。它让房间 `1` 的“左 7、上 18”在第 4 次成功，原先成功的六项尝试次数均未增加。这些成绩来自上述固定起点和目标。

剩余失败项是 `6a` 的“左 6、上 37”。已枚举的 147 种两段组合中，唯一成功路线是**向右跳 10 帧，再向左上冲刺 8 帧**。正确动作分别排第 5/7 和第 20/21，都缺少已学评分。下一项实验是从邻近起点补充经验，再用原起点检验排名与抵达率。

## 第一次运行

在仓库根目录使用 Python 3.12 或更高版本。先运行模拟后端与测试，无需启动游戏或配置模型密钥。

```powershell
python -m pip install -e ".[dev]"
python -m agent.main --mode mock --steps 1
python -m pytest tests -q
```

实机运行需要 Celeste、Everest 和桥接 Mod。桥接当前版本为 **0.3.10**，使用 .NET 8 构建。实机搜索依赖 SpeedrunTool 的存档与恢复能力；运行 TAS 相关功能时还需要 CelesteTAS。

```powershell
$env:CELESTE_DIR = 'F:\steam\steamapps\common\Celeste'  # 换成本机游戏目录
./scripts/build_bridge.ps1
```

将生成的 `bridge/CelesteAgentBridge/bin/Release/net8.0/CelesteAgentBridge.dll` 与同项目下的 `everest.yaml` 放入游戏的 `Mods/CelesteAgentBridge/`，完全退出并重启游戏。进入可操作的房间后，通过本地 TCP 端口 `127.0.0.1:32123` 检查连接。

```powershell
python -m agent.inspect --what player
python -m agent.room_map --objects
```

使用 DeepSeek 规划时，先在环境中配置 `DEEPSEEK_API_KEY`。例如，从 6b 起点试运行协同规划与执行：

```powershell
python -m agent.main --real --mode deepseek --collab --exit-room 6b --success-room 6c --steps 12
```

## 本地策略如何复测

`agent.local_policy` 从实机搜索报告中学习动作回报。表的状态包括相对目标位置、速度、着地状态和剩余冲刺次数，动作键同时包含按键和持续帧数。`agent.live_search` 从存档逐次执行候选并记录结果。束宽决定保留多少条分支，预算决定最多实际尝试多少次。

下面的 PowerShell 命令使用本机实验中的冻结策略表。从房间 `1` 的已测试起点 `(19, 144)` 出发，可复测“左 7、上 18”。目标偏移以像素计，向上的纵向偏移为负数。

```powershell
python -m agent.live_search --dx -7 --dy -18 --radius 6 --frames 10 --second-frames 10 9 8 --depth 2 --min-segments 2 --width 2 --top-k 3 --budget 8 --no-checkpoint --policy logs/three-checks-frozen-policy.json --continuation-ranking --terminal-jump-fallback --output logs/navigation-retest.json
```

`--no-checkpoint` 让候选共享同一起点；`--continuation-ranking` 依据下一步的最高已学评分筛选第一层分支。变长时长与普通动作共享同一预算。复测出口时使用 `--exit-room 6`，由房间切换判定成功。

`logs/` 中的冻结表、训练报告和实机结果属于本地产物。新检出仓库时，可以先省略策略及其专用开关来采集基线报告，再用自己的报告训练一张表。要复现上表的成绩，需要相同的冻结表、游戏起点和搜索配置。

```powershell
python -m agent.live_search --dx 24 --dy 0 --radius 6 --frames 10 --depth 2 --budget 24 --no-checkpoint --output logs/training-run.json
python -m agent.local_policy logs/training-run.json --output logs/my-policy.json
```

## 代码与记录

| 入口 | 适合查什么 |
| --- | --- |
| [游戏桥接](bridge/CelesteAgentBridge/) · [协议文档](docs/protocols.md) | 状态观测、帧级输入、存档恢复、截图与 TAS 接管 |
| [规划入口](agent/main.py) · [协作执行](agent/agents/) | 模型规划、动作契约、本地重试与已验证路线重放 |
| [本地搜索](agent/live_search.py) · [策略训练](agent/local_policy.py) | 候选排序、预算、蒙特卡洛回报与零覆盖状态补样 |
| [分段执行](agent/step.py) · [动作枚举](agent/sweep.py) · [TAS 回放](agent/tas_play.py) | 手动校准动作、从同一存档比较路线、按房间重放输入 |
| [验收记录](docs/acceptance.md) · [阶段记录](docs/phase4-7-status.md) · [房间 7 种子路线](agent/agents/seed-routes.json) | 早期实机验证、阶段边界及可重放的动作序列 |

最新评测的逐次执行记录位于本机 `logs/left-up-terminal-jump.json`。与 `logs/left-up-route-audit.json` 对照，可以查看同一目标的候选动作、实际执行顺序和首次抵达次数。
