[English](README.md) | **简体中文**

# clawtouch-mcp

> **给 LLM agent 装一双真实的手。**
> 一个 MCP server, 把 [Claude Desktop](https://claude.ai/download) /
> [Cline](https://github.com/cline/cline) / [Continue](https://github.com/continuedev/continue) /
> [Cursor](https://www.cursor.com/) / [OpenClaw](https://github.com/openclaw) /
> [Hermes Agent](https://github.com/NousResearch/hermes-agent) 等任何 MCP 兼容客户端,
> 变成能透过 USB HID 设备移动真实鼠标、按下真实按键的执行器。

🌐 **[clawtouch.cn](https://clawtouch.cn)** — 官网,购买硬件 / 查文档 / 商务咨询都在这里。

[![PyPI version](https://img.shields.io/pypi/v/clawtouch-mcp.svg)](https://pypi.org/project/clawtouch-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/clawtouch-mcp.svg)](https://pypi.org/project/clawtouch-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 这是什么?

一个独立的 Python 进程,通过 stdio 跟 **Model Context Protocol** (MCP) 客户端
通信,把鼠标/键盘原语暴露给上层的 LLM agent。底层走 USB 串口跟一块 **ClawTouch
HID 设备**(基于 Raspberry Pi Pico 2,运行 [开源 ClawTouch HID 固件](#硬件))
对话,把 agent 发来的 `hid.click` / `hid.type` / `hid.scroll` 工具调用翻译
成 HID 报告,走标准 OS HID 驱动栈下发到目标机。

**为什么有用?** 大部分"AI 控制电脑"的 demo 都要求在目标机上跑 agent 进程,
并走 OS 层合成输入 API。这类路径在 kiosk 锁机环境、嵌入式测试台架、跨设备
RPA 等"目标机必须保持干净"的场景里有局限。USB HID 物理外设走标准 OS HID
驱动栈,跟任何插上的键盘鼠标走同一条数据通路 —— 目标机零安装,无 agent
进程,无事件注入层。

> 📦 MIT 协议。不依赖 ClawTouch 后端、不带 LLM、不带输入节奏修饰层。
> 纯粹的 HID 管道,让其他 agent 框架能直接对接真实硬件。

## 适用范围 —— 能干什么不能干什么

**一台设备只对应一个目标。** 硬件是 USB 外设,只有单一宿主连接。
你插哪台机器,它就只能驱动那台。这是设计本身决定的 —— 这是一个
**对位控制设备,不是群控工具**。要控 10 台机器你得买 10 个设备。

我们支持这些场景:

- **RPA / 测试自动化** —— 给装不了软件的老机器、kiosk 锁机壳、
  跑不支持系统的工控机、或 QA 实验室里的手机机柜对接 AI agent。
- **无障碍辅助** —— 让残障用户用 LLM agent 发 HID 指令操控自己
  电脑,不用跟各应用的合成输入兼容性死磕。
- **兼容性测试** —— 验证你的软件对外接 HID 输入的处理是否正确
  (跟注入合成事件可能有差异)。
- **跨机工作流** —— 一台开发笔记本上的 agent 控机柜里的测试机,
  目标机零安装 agent。

我们**不支持、不文档化、不协助**这些场景:

- **消费平台的批量账号注册 / 多账号运营**(微信、抖音、Instagram 等)
  —— 单设备单宿主结构上就不适合,且在多数司法辖区属于监管红线。
- **针对特定应用的脚本化适配层**(选择器、固定流程脚本)
  —— 这些应该在上层 agent / RPA 框架做,本仓库只做底层 HID 原语。

如果你想干的是上述两类事,这不是合适的工具,我们也帮不上忙。

## 安装

```bash
pip install clawtouch-mcp                 # 最小依赖 (只装串口)
pip install 'clawtouch-mcp[screenshot]'   # 加装 mss 启用 hid.screenshot
```

**macOS 用户**: 看 [`docs/macos-setup.md`](docs/macos-setup.md) — 平台特定坑
(首次插 Pico 弹的键盘助理对话框 / 双 USB-CDC 端口 / Screen Recording
权限 / 输入法不匹配引发的 type 乱码).

## 运行

```bash
# 1. 自动探测 HID 板, 限制鼠标活动范围在 1920×1080 屏幕内
clawtouch-mcp --screen 1920x1080

# 2. 显式指定端口 (Windows)
clawtouch-mcp --port COM7 --screen 1920x1080

# 3. 没有硬件 - 全部操作只打印日志, 不实际执行 (开发/CI 模式)
clawtouch-mcp --mock --log-level INFO
```

## 接入 Claude Desktop

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) 或 `%APPDATA%\Claude\claude_desktop_config.json` (Windows),
加入:

```json
{
  "mcpServers": {
    "clawtouch": {
      "command": "clawtouch-mcp",
      "args": ["--port", "COM7", "--screen", "1920x1080"]
    }
  }
}
```

重启 Claude Desktop,在 MCP server 列表里能看到 `clawtouch`,带 9 个可用
工具。试一下:

> 帮我截屏,找到搜索框,点一下并输入 "hello world"。

(`hid.screenshot` 工具默认关闭,需要加 `--allow-screenshot` 启用 — 隐私安全
考虑。)

## 跟其他 MCP 客户端集成

7 家已验证客户端的可直接复制 config 在 [`examples/integrations/`](examples/integrations/):

- [Claude Desktop / Claude Code](examples/integrations/claude_desktop.md)
- [Cursor](examples/integrations/cursor.md)
- [OpenClaw](examples/integrations/openclaw.md)
- [Hermes Agent (NousResearch)](examples/integrations/hermes_agent.md)
- [ChatGPT Desktop + OpenAI Codex CLI](examples/integrations/openai.md)
- [Cherry Studio 🇨🇳](examples/integrations/cherry_studio.md)
- [Trae IDE (字节跳动) 🇨🇳](examples/integrations/trae_ide.md)

每份文件都含连通性验证 + 常见坑。欢迎 PR 加新客户端。

## 接 Computer Use 循环

如果你不是接 MCP 客户端, 而是自己写 Computer Use 循环, 看
[`examples/computer_use/`](examples/computer_use/) 两份参考实现 ——
把 Anthropic / OpenAI agent 的动作路由到 ClawTouch HID:

- [Claude Computer Use → HID](examples/computer_use/claude_demo.py) ——
  `client.beta.messages.create` 配合 `computer_20250124` 工具
- [OpenAI CUA → HID](examples/computer_use/openai_cua_demo.py) ——
  Responses API + `computer-use-preview`

两份 demo 都直接 import `clawtouch_mcp.bridge.SerialHidBridge` (不走 MCP
子进程), 单机跑.

## 应用 skill (给 LLM 的具体软件操作指南)

[`clawtouch-skills`](https://github.com/tinqiao-oss/clawtouch-skills)
是姊妹仓 —— markdown skill 文件集, 针对具体应用写的操作手册,
LLM 在驱动该应用前 load 进 context 用。首批覆盖 LLM 训练数据稀疏
的国内软件:

- WPS Office、飞书 / Lark、钉钉 —— 见
  [`tinqiao-oss/clawtouch-skills`](https://github.com/tinqiao-oss/clawtouch-skills)

Skill 是软性指导 (LLM 仍然自己决定怎么走), 不是确定性执行流程。
要可审计 / 可重放 / 带 SLA 的确定流程, 见
[`docs/COMMERCIAL_PRODUCT.zh-CN.md`](docs/COMMERCIAL_PRODUCT.zh-CN.md)。

## 工具清单

| 工具              | 用途                                          |
|-------------------|-----------------------------------------------|
| `hid.click`       | 在绝对坐标 (x, y) 点击                        |
| `hid.move`        | 移动鼠标 (绝对/相对)                          |
| `hid.hover`       | 移动后停留                                    |
| `hid.type`        | 输入 UTF-8 字符串                             |
| `hid.scroll`      | 滚轮滚动 (正数下滚 / 负数上滚)                |
| `hid.key`         | 命名键 / 快捷键 (`enter`, `ctrl+c` 等)        |
| `hid.release_all` | 紧急停止 — 释放所有按住的按键和鼠标键         |
| `hid.screenshot`  | 主显示器 PNG 截屏 (默认关闭,需显式启用)       |
| `device.list`     | 列出候选 HID 板串口                           |
| `device.info`     | 当前连接信息                                  |

## 安全策略

* 坐标会被 `--screen WxH` **clamp 截断**,防止 agent 把鼠标移到屏幕外
* 单次输入文本**最多 4096 字符**
* 所有操作受 `--ops-per-sec` 速率限制(默认 20 次/秒)
* `hid.screenshot` **默认禁用**,加 `--allow-screenshot` 才启用
* `hid.release_all` 暴露给 agent 作为紧急停止手段

## 硬件

本 server 能跟两种硬件对话:

1. **ClawTouch HID 设备** — 成品硬件,即插即用。咨询/订购请去
   [clawtouch.cn](https://clawtouch.cn)。
2. **任何刷了 [clawtouch-hid-firmware](#开源路线图) 的 RP2350 板** —
   即将作为独立开源仓库发布。买一块 Pico 2(树莓派官方 ¥55),烧固件,就能用。

线协议两种硬件完全一致,本 server 不区分。

## 常见问题

**需要 ClawTouch 账号 / API key / 云服务吗?**
不需要。本 server 只通过 USB 串口跟硬件通信,**没有任何网络请求**,数据
不出本机。

**没有 ClawTouch 硬件能用吗?**
等 `clawtouch-hid-firmware` 仓库发布后(见路线图),买一块 ¥55 的
Raspberry Pi Pico 2(树莓派官方价)烧固件就能用。当前要么买成品硬件,
要么等固件发布。

**为啥用 HID 而不是 OS 级 API (pyautogui 等)?**
OS 级合成输入在某些受限环境(企业 EDR、kiosk 锁机、辅助技术兼容性等)中
可能不被允许或不被识别。HID 输入走系统标准 HID 驱动栈,**跟外接物理
键盘鼠标走完全相同的输入路径**。这点对辅助技术兼容性测试、kiosk 自动化、
离线测试台架、跨设备 RPA 等场景至关重要。

**能控制手机吗?**
能 —— HID 设备是宿主无关的。通过 USB-OTG 把它插到手机(安卓或 iOS)上,
同一套 `hid.*` 工具照常工作,手机看到的就是一个普通 USB 键鼠。无线接法
(板子插手机、由 PC 通过 Wi-Fi 发命令)见 `clawtouch-hid` 仓的
[Wi-Fi 传输指南](https://github.com/tinqiao-oss/clawtouch-hid/blob/master/docs/wifi-transport.zh-CN.md)。
开箱即用、由感知驱动的手机自动化属于商业产品,不在本 server 范围内。

**有 JavaScript / TypeScript 版本吗?**
暂时没有。`clawtouch-bridge-sdk`(Python + Node 双语言)在规划中 — 见路线图。

**跟闭源的 ClawTouch 桌面端有什么区别?**
本 MCP server 只是最底层 —— 纯 HID 原语, 让其他 agent 框架能在不绑定
ClawTouch 完整产品的前提下使用硬件。→ 桌面端额外做什么 (视觉识别 /
多步编排 / 应用适配器 / B2B 层), 完整分层对比 + 截图见
[`docs/COMMERCIAL_PRODUCT.zh-CN.md`](docs/COMMERCIAL_PRODUCT.zh-CN.md)。

## 开源路线图

ClawTouch 采用 **open-core** 模式:硬件与协议层开源,集成的商业产品闭源。

| 组件                                              | 状态                  |
|---------------------------------------------------|-----------------------|
| **clawtouch-mcp**                                 | ✅ 已发布 (本仓库)    |
| **clawtouch-hid-firmware** (CircuitPython, RP2350)| 🟡 即将发布           |
| **clawtouch-hid-protocol** (USB-CDC 协议规范)     | 🟡 即将发布           |
| **clawtouch-bridge-sdk** (Python + Node SDK)      | 🔵 规划中             |
| 后端服务 / 桌面端 / 应用适配器 / 视觉模型         | 🔒 闭源 — [详细对比](docs/COMMERCIAL_PRODUCT.zh-CN.md) |

不设硬性日期 — 每个组件打磨好了再发。关注组织
[@tinqiao-oss](https://github.com/tinqiao-oss) 接收更新通知。

## 架构总览

```
┌─────────────────────┐       stdio JSON-RPC      ┌─────────────────────┐
│ Claude Desktop /    │ ◄──────────────────────► │  clawtouch-mcp      │
│ Cline / OpenClaw    │                          │  (本仓库)           │
└─────────────────────┘                          └──────────┬──────────┘
                                                            │ USB serial (CDC)
                                                            ▼
                                                 ┌─────────────────────┐
                                                 │  Pico 2 + ClawTouch │
                                                 │  HID 固件           │
                                                 └──────────┬──────────┘
                                                            │ USB HID
                                                            ▼
                                                 ┌─────────────────────┐
                                                 │  你的操作系统       │
                                                 │  (Win/Mac/Linux)    │
                                                 └─────────────────────┘
```

完整的"一次 `hid.click` 调用如何从 LLM 一直流到 Pico USB HID 端点"的
逐字节技术分析,见 [`docs/TECHNICAL_WALKTHROUGH.zh-CN.md`](docs/TECHNICAL_WALKTHROUGH.zh-CN.md)。

想看更大的图景 —— 这个 MCP server 在 ClawTouch 完整的"感知 → 决策 →
执行"循环里处于什么位置、数据怎么流、闭源桌面端是怎么搭在开源 HID
原语上的 —— 请看官方技术文档:

* [系统架构 + 数据流](https://clawtouch.cn/docs/architecture.html) — 三层执行模型,以及与 RPA / AutoHotkey / 浏览器扩展自动化的工程差异
* [数据安全 + 合规](https://clawtouch.cn/docs/security.html) — 哪些在本地、哪些过网、哪些加密

## 参与贡献

欢迎 PR:新增 MCP 工具(映射现有 HID 原语)、Bug 修复、增加客户端集成示例、
文档改进、非中文 README 翻译。

**不接受** 的 PR:输入节奏修饰 / 行为模拟层相关功能(故意排除在范围外 —
见[开源路线图](#开源路线图)),特定应用的适配器(这部分在闭源桌面端)。

## 关于项目

`clawtouch-mcp` 由 **北京亭桥科技** 维护 —— ClawTouch 产品团队
([clawtouch.cn](https://clawtouch.cn)),做即插即用的 USB 设备,让 LLM
agent 在 HID 层操控真实的 Windows / macOS / Linux 桌面。本 MCP server
是整个产品栈最底层、最通用的那部分 — 哪些开源、哪些闭源详见
[开源路线图](#开源路线图)。

## License

MIT © 北京亭桥科技有限公司. 见 [LICENSE](LICENSE).

商业部署 / 企业支持 / OEM 硬件合作咨询:`support@tinqiao.com`
