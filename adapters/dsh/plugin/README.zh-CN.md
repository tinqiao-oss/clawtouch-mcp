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

硬件是树莓派官方 **Pico 2** (约 ¥55) + 开源固件
[clawtouch-hid](https://github.com/tinqiao-oss/clawtouch-hid)。按那个仓库的说明把固件拖进
BOOTSEL 盘即可。

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

### 3. 一个能看图的模型 (在 dsh 里配置, 不在本插件里)

本插件**不带模型、不读任何 API key**。负责「看截图、说目标在哪」的模型(下称「眼睛」)
就是你在 dsh 里配好的模型: 不做任何配置时, 用**当前对话正在用的模型**; 也可以在插件配置里
用 `vision.provider` + `vision.model` 指定 dsh 里的另一条模型路由(在插件的设置页里直接选也行)。这个模型必须声明能看图,
否则插件会在按下任何东西之前拒绝。不同模型点得准不准差别很大, 见下面「怎么选眼睛」。

---

## 安装

```bash
pip install 'clawtouch-mcp[screenshot,window]>=0.5.1'
dsh plugin --profile <你的 profile> add dsh-clawtouch
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

## dsh 版本、dsh 自带的 Computer Use

0.2.0 需要 dsh 0.2.x(眼睛走 dsh 自己的模型服务和附件服务)。2026-11-14 在 dsh 0.2.2-rc.1 上验证过: 工具走原生执行器和 PTC(模型在 `run_code`
里调用 `tools.computer_*`, 拿到结构化结果)都能用, 组合键拦截对 PTC 程序里的调用
同样生效。dsh 自己也带一个实验性的 Computer Use —— 一个 profile 里**两者只开一个**:
本插件的组合键拦截只管得到它自己的工具。

[clawtouch-hid](https://github.com/tinqiao-oss/clawtouch-hid) 固件 1.2 加了绝对坐标
指针: 在 Windows 上 `clawtouch-mcp` 会一步把光标放到主屏的目标像素上, 不再反复
小步挪动。旧固件照旧能用, 只是走原来的方式。

---

## 设置页

在 dsh 里打开 **插件 → dsh-clawtouch**,说明下面就是:

- **状态**:打开页面时查一次,也可以点「重新检查」,不按任何东西。显示现在用哪个模型看图
  (指定了模型的话,还会显示 dsh 说它能不能看图)、`clawtouch-mcp` 在不在运行和版本、板子
  回不回话、固件版本、移动有没有用上绝对坐标。查板子要 `clawtouch-mcp` 0.6.1 或更新。
- **看图模型**:跟随当前对话的模型,或者 dsh 里任何声明了能看图的模型;实测过的模型旁边标了命中率。
- **只看不按**(`dryRun`),以及「高级」里的截图最大宽度(`maxWidth`)和 `clawtouch-mcp`
  的路径(`command`)。
- 配置里还留着 0.1.x 的 `vision.endpoint` / `apiKey` / `extraBody` 时,页面只写出键名
  (不显示值),并有一个按钮只删这几项。

保存会让插件重新加载,所以 AI 正在点击或打字时不让保存;保存写入期间新开始的操作会在按下任何东西
之前被拒绝。保存只改页面上这几项,这一行配置里的其他键原样保留。页面上没有的设置(包括按键拦截,
这是有意不放的)仍在 profile 的 `cordis.patch.yml` 里改。

---

## 怎么选眼睛

眼睛就是看每张截图、说出目标在哪的那个模型。**它决定了点错的频率**,是最要紧的一项设置。
离线实测, 按 dsh 实际递图的方式(模拟了 dsh 插在图片前的那行说明), 用插件自己的提示词和标定方法(源码仓 `tools/vision-bench`,
2026-11-14; 每个模型 3 轮共 165 次;「不存在的目标」共 24 个, 是屏幕上没有、本该说找不到的目标):

| 眼睛(经 dsh) | 命中 | 点错 | 不存在的目标也去点 | 单次中位 |
|---|---|---|---|---|
| `qwen3.8-max`(关思考) | 96.4% | 2.4% | 0 | 2.0 秒 |
| `qwen3-vl-plus` | 96.4% | 3.6% | 3 | 1.8 秒 |
| `deepseek-flash`(关思考, dsh 默认模型) | 78.2% | 21.8% | 1 | 0.9 秒 |
| Claude、GPT 等 | 未测 | | | |

用 DeepSeek 对话、又什么都不配时, 大约每 5 次点击就有 1 次点到目标旁边的东西。每条结果都会
写明这次是哪个眼睛看的(`eye 路由/模型`), 方便发现。一次看多个目标时(`computer_click_sequence`),
`qwen3.8-max` 更常回答「找不到」而不是乱猜(87% 的目标找到, 点错 1.8%), 这时整串点击会被拒绝,
不会只点一半。真机端到端(经 dsh 0.2.2-rc.1, DeepSeek 当手, 计算器 7+9 与画图「铅笔 + 红色」,
每轮结束后用 UI Automation 独立核对; 样本少, 以上表为准): `qwen3.8-max` 5 次成功 4 次(失败那次任务窗口
被别的程序完全挡住, 插件拒绝在它上面操作), `qwen3-vl-plus` 4 次全成功, `deepseek-flash` 5 次成功 3 次。

为什么强调「按 dsh 实际递图的方式」: dsh 会在每张图片前插一行文字说明它的像素尺寸, 旧提示词下
`qwen3.8-max` 因此把两套坐标系混着用, 命中从 98% 掉到 64%。0.2.0 起提示词让标定块和目标统一用
0–1000 坐标, 准确率回来了。

在 dsh 里接阿里云百炼的配置(用 `dsh-llm-pi-ai`), 可直接贴进 profile 的 `cordis.patch.yml`。
**两行 `compat` 必须写**: 不写的话百炼直接拒绝请求, 或者不关思考、看一次要 45 秒以上(实测):

```yaml
- id: llm-pi-ai
  config:
    providers:
      bailian:
        api: openai-completions
        baseURL: https://dashscope.aliyuncs.com/compatible-mode/v1
        apiKeyEnv: DASHSCOPE_API_KEY
        compat:
          thinkingFormat: qwen
          supportsDeveloperRole: false
        models:
          - id: qwen3.8-max
            input: [text, image]
            reasoningEfforts:
              off:
              high: high
- id: clawtouch
  config:
    vision:
      provider: bailian
      model: qwen3.8-max
```

两个成本要知道: 每次看图, 截图都会存成 dsh 的附件, 而 dsh 的附件永不删除 —— 实测窗口截图
每张 20–70 KB, 每天点 100 次大约多几 MB; 另外看图是一次单独的模型调用, 它的 token 不计入
对话本身的用量统计。

**从 0.1.x 升级:** `vision.endpoint`、`vision.apiKey`、`vision.extraBody` 不再读取,
插件也不再读 `DASHSCOPE_API_KEY`。配置里还留着这些键时, 看图类工具会直接拒绝并说明怎么改
(什么都不按), 不会悄悄换成别的模型。删掉它们, 然后要么不写 `vision`(用当前对话的模型),
要么照上面指向 dsh 里的一条路由。

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
  Alt+F4 / Ctrl+W / Ctrl+F4 / Alt+空格(窗口菜单, 下一个键就能关窗口)。一次真实的 HID 按键落在拥有焦点的任何窗口上 ——
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
