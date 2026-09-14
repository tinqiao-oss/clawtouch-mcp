[English](README.md) | **简体中文**

# dsh-clawtouch

> **告诉它点什么,它就用真鼠标点下去。**
> 一个 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 插件,
> 让 agent 拥有一套物理 USB HID 键鼠 —— 用人话指挥,而不是给坐标。

```
computer_click({ target: "那个蓝色的发送按钮" })
```

它会截取当前窗口、让视觉模型指出目标在哪、把图上坐标换算回屏幕坐标,
然后**通过一块真实的 USB 硬件**把鼠标移过去、按下去。

---

## ⚠️ 先看这里:三个前置条件,缺一不可

这个插件**不是**纯软件方案。它的全部意义就在于输入是**物理的** ——
所以下面第一条没得商量。

### 1. 一块烧好 ClawTouch HID 固件的硬件 (必须)

插件自己不产生任何输入事件。它把指令交给 `clawtouch-mcp`,后者通过 USB 串口
发给一块单片机,由**那块硬件**输出标准 USB HID 报告。目标机看到的是一套真实
插上的键鼠,走的是和任何外设完全相同的驱动通路。

两种来源,协议完全一致,插件不区分:

| 来源 | 说明 |
|---|---|
| **成品 ClawTouch 设备** | 开箱即用。咨询/订购 → [clawtouch.cn](https://clawtouch.cn) |
| **自己烧一块** | 树莓派官方 **Pico 2** (约 ¥55) + 开源固件 [clawtouch-hid](https://github.com/tinqiao-oss/clawtouch-hid)。按那个仓库的说明把固件拖进 BOOTSEL 盘即可 |

> **能不能不用硬件?** 目前不能:插件的每一次点击和按键都经这块板子发出,
> 没有它就只能看、按不了任何东西。不过「看得准不准」可以先验:配置里开
> `dryRun: true`,只定位、报告坐标,什么都不按,不插板子也能用。
> (`mock: true` 只用来测试插件和 `clawtouch-mcp` 之间的连线,同样什么都不按,
> 每条结果都会写明。)

### 2. Python 端 `clawtouch-mcp` (必须)

```bash
pip install 'clawtouch-mcp[screenshot,window]>=0.5.1'
```

**两个 extra 都要,不能只写 `[screenshot]`** —— 后者只有 mss + Pillow,
不含 pyobjc。macOS 上少了它,窗口列表功能直接不可用。

### 3. 视觉模型的 API key (必须,除非只做硬件自检)

```bash
export DASHSCOPE_API_KEY=sk-...
```

默认用阿里云百炼的 `qwen-vl-max`。换别的模型见下面「配置」。

---

## 安装

```bash
pip install 'clawtouch-mcp[screenshot,window]>=0.5.1'
dsh plugin --profile <你的 profile> add dsh-clawtouch
export DASHSCOPE_API_KEY=sk-...
```

`@deepseek-ai/dsh-tools` 由宿主提供,插件刻意**不**声明这个 peer 依赖:
dsh 还在发 rc 版本,而 semver 规定预发布版本只被带相同预发布标识的范围匹配,
没有任何范围能匹配得上正在用的那个版本 —— 声明了反而会让 `npm install` 失败。

---

## 叫 agent 之前,先自己验一遍

**分三层,缺前置条件也能验前面几层。** 强烈建议按顺序走完再让 agent 上手,
否则出问题时你分不清是硬件、截图、密钥还是坐标算错了。

```bash
# 第 1 层 —— 不需要硬件、不需要密钥:它能看见你的窗口吗?
node probe.js --windows

# 第 2 层 —— 需要硬件,不需要密钥:光标落点准不准?
node probe.js --move-test

# 第 3 层 —— 需要硬件 + 密钥:整条链路
node probe.js "计算器的等号键" --window 计算器
node probe.js "计算器的等号键" --window 计算器 --click
```

**第 2 层是最值钱的一层**:它把光标驱动到每个校准 marker 自己的屏幕位置,
报告实际偏差多少像素。**这一层不花任何 API 费用**,而且一旦这里不对,
问题一定在视觉层之下 —— 不用再往上查。

多显示器:`clawtouch-mcp` 默认只按**主显示器**钳制坐标,副屏上的窗口够不到。
把整个虚拟桌面尺寸告诉它:

```bash
node probe.js --windows --screen 7680x1440
```

⚠️ 副屏摆在主屏**左边或上边**时坐标是负的,而 `--screen` 只有尺寸没有原点 ——
再大的 `WxH` 也够不到。把副屏挪到右边或下边即可。

---

## 工具

| 工具 | 作用 |
|---|---|
| `computer_click` | 用一句话描述目标,点它 |
| `computer_click_sequence` | 看一次,连点多个目标 (省往返) |
| `computer_find` | 只定位不点击 |
| `computer_windows` | 列出窗口,拿到准确标题 |
| `computer_type` | 打字 (可先点目标再打) |
| `computer_key` | 按键与组合键 |
| `computer_scroll` | 滚动 |

---

## 平台支持:这是一张表,不是一句承诺

| | Windows | macOS | Linux |
|---|---|---|---|
| 窗口列表 + 矩形 | ✅ | ✅ (需 pyobjc) | ❌ |
| 把截图裁到单个窗口 | ✅ | ✅ | 只能手给 region |
| **自动把后台窗口抬到前面** | ✅ | ❌ | ❌ |
| **遮挡测量** (被别的窗口盖住多少) | ✅ | ❌ | ❌ |
| **模态守卫** (窗口是否根本不收输入) | ✅ | ❌ | ❌ |

后三项在 macOS 上**不是坏了,是刻意没有**。测不出来的东西,返回里就**不会有
那个字段** —— 绝不会填一个看起来正常的值。字段缺失表示「没测」,不表示
「测过没问题」。

Linux 没有窗口列表,只能截整个桌面 —— 实测在多显示器下定位准确率是 0/6,
所以**当作不支持**。

---

## 安全

- **键盘输入进的是当前有焦点的窗口。** 先点输入框,或者给 `computer_type`
  传 `target` 让它替你点。
- **退出类组合键默认被拦截**:macOS 上 Cmd+Q / Cmd+W;Windows 与 Linux 桌面上
  Alt+F4 / Ctrl+W / Ctrl+F4。一次真实的 HID 按键落在拥有焦点的任何窗口上 ——
  如果那是 agent 自己的窗口(包括浏览器里的 dsh 网页,Ctrl+W 就关掉它),会话当场
  结束。确实要放开就在配置里 `allowQuitCombos: true`。
- **切换窗口的组合键也默认被拦截**:Windows 与 Linux 桌面上 Alt+Tab、Alt+Esc、
  Ctrl+Esc、Ctrl+Alt+Del 和所有 Win/Super 键组合;macOS 上 Cmd+Tab、Cmd+空格、
  Cmd+\`、Cmd+H、Cmd+M、Cmd+Option+Esc、Ctrl+方向键、Ctrl+F2/F3(Cmd+C 这类普通
  应用快捷键不受影响)。真实按键跟着焦点走,焦点一离开任务窗口,之后的每一个键都会
  落到别处 —— 实测中 agent 按了一次 Alt+Tab,整场再没回来。要操作别的窗口:Windows
  上给 `computer_click` 传 `window`,它会点标题栏把窗口抬到前面;其他平台点那个窗口
  露出来的部分。被控的不是运行 dsh 的这台机器时,才在配置里
  `allowFocusSwitchCombos: true`。
- 两道拦截都按 `clawtouch-mcp` 的解析方式读组合键:`key: "alt+tab"` 就是 Alt+Tab,
  首尾空白按 Python 的规则去掉,数字键按数字读。
- **打字走美式键盘布局,一个字符按一个键。** 中文、emoji、弯引号在这个布局上
  没有对应的键,`computer_type` 遇到这类文字会在点击和打字之前整段拒绝,不会
  打到一半才停。开着中文输入法时,打出的字母可能变成候选词、标点变成全角,
  打完要核对。
- **`dryRun: true`** 只定位、报告坐标,什么都不按 —— 连把后台窗口抬到前面的那一下
  标题栏点击也不点,所以被挡住的窗口会直接拒绝。新机器上先用它跑一遍,不插板子
  也能用。
- **抬窗口是点它的标题栏,不是抢焦点 API。** 而且点完会重新读一次窗口状态 ——
  确认不了就如实报告,不假装成功。
- 这等于把一台机器的真实键鼠交给 agent,**和一个人坐在键盘前是一回事**。

---

## 协议

MIT。详见 [LICENSE](LICENSE)。
