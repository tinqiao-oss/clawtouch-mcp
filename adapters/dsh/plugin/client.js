/**
 * dsh-clawtouch, browser half: the package's own page in dsh's Plugins page.
 *
 * Two parts. A status card — which model looks at the screen, whether
 * clawtouch-mcp runs, whether the board answers and with what firmware —
 * checked when the page opens and again on request, pressing nothing. And a
 * form for the settings people actually change: the eye, "look, don't press",
 * and under Advanced the capture width and the clawtouch-mcp path. Everything
 * else stays in the profile's YAML, the key guards on purpose.
 *
 * The page talks only to the routes index.js registers (lib/routes.js). It
 * never sees a value it does not edit: the 0.1.x API key is named, not shown.
 *
 * Plain script, no build step: React comes from the page's module table, no
 * Harness client package is loaded as a module (they change without notice),
 * and the colours are the host's theme tokens, so light, dark and third-party
 * themes follow. Visible text goes through the locale service.
 */
window.__ModuleLoader__.load({
  id: 'dsh-clawtouch',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useCallback, useEffect, useRef, useState } = React

    const NS = 'clawtouch'
    /** The Plugins page keys a package's own settings by its package name. */
    const PACKAGE = 'dsh-clawtouch'
    /** Relative to the page, as dsh's own pages fetch, so a mounted deployment keeps its prefix. */
    const SETTINGS_API = 'api/clawtouch/settings'
    const STATUS_API = 'api/clawtouch/status'
    const MODELS_API = 'api/clawtouch/models'
    const SETUP_API = 'api/clawtouch/setup'
    const INSTALL_API = 'api/clawtouch/install'
    /** This plugin's version, for the footer; a test keeps it equal to package.json's. */
    const VERSION = '0.4.0'
    const ISSUES = 'https://github.com/tinqiao-oss/clawtouch-mcp/issues/new'
    /** Hit rates from the README's "Choosing the eye" (offline, 165 looks per model,
     *  dsh's image note simulated, 2026-11-14). Anything not here was not measured. */
    const MEASURED = { 'qwen3.8-max': 96, 'qwen3-vl-plus': 96, 'deepseek-flash': 78 }
    const DEFAULT_WIDTH = 1600
    const WIDTH_RANGE = [320, 7680]
    const DEFAULT_COMMAND = 'clawtouch-mcp'
    /** How long a save waits, in all, to read its settings back from the reloaded plugin. */
    const READ_BACK_MS = 10000
    /** The clawtouch-mcp this plugin needs; a test keeps it equal to lib/installer.js's MCP_VERSION. */
    const REQUIRED_MCP = '0.6.1'

    const zh = {
      summary: '看图模型、只看不按、设备状态。',
      loading: '正在读取设置…',
      loadFailed: '读不到设置。',
      retry: '重试',
      unavailable: '这个 dsh 不能在页面里保存设置（没有配置编辑服务，或找不到插件自己那一行）。请改配置文件 cordis.patch.yml，写法见插件说明。',
      expression: '配置里有设置是用 !!js 表达式写的，这里显示不了它的实际值，所以只读。请直接改 cordis.patch.yml。',
      statusTitle: '状态',
      recheck: '重新检查',
      checking: '正在检查…（不会按任何东西）',
      checkFailed: '检查没有完成。',
      eyeLabel: '看图',
      eyeFollow: '跟随当前对话的模型。能不能看图、准不准，取决于对话里选的那个模型。',
      eyeRoute: '{route}',
      imageYes: '能看图',
      imageNo: 'dsh 说它不能看图：点击会被拒绝，请换一个模型',
      imageUnknown: '查不到它能不能看图',
      eyeProblemTimeout: 'dsh 5 秒内没答复。',
      eyeProblemFailed: 'dsh 说不出这个模型的信息：名字可能写错了，或者这个服务商没配好。',
      eyeProblemNoLlm: '这个 dsh 没有模型服务。',
      eyeIncomplete: 'vision.provider 和 vision.model 只填了一个：看图会被拒绝。在下面重新选一次。',
      legacyStatus: '配置里还有 0.1.x 的旧设置，看图会被拒绝（见下方）。',
      mcpLabel: 'clawtouch-mcp',
      mcpFound: '在运行，版本 {version}',
      mcpRunning: '在运行',
      mcpMissing: '运行不了「{command}」。可以用下面的「安装」，已经装过的话在「高级」里填它的完整路径。',
      boardLabel: '板子',
      boardConnected: '已连接 {port}，固件 {firmware}',
      absOn: '绝对坐标：开（主屏上移动一步到位）',
      absOff: '绝对坐标：关（{reason}）',
      boardAbsent: '没连上。',
      boardSilent: '{port} 打开了，但板子没有回应：拔插一次再查。',
      boardBusy: 'AI 正在用板子，这次没去打扰它。等这一步做完再点「重新检查」。',
      boardNoAnswer: '等了 8 秒没有回应。',
      boardOldServer: '这个 clawtouch-mcp 太旧，查不了板子。升级到 0.6.1 或更新：pip install -U clawtouch-mcp',
      boardOldServerPort: '（它说用的是 {port}）',
      boardMock: '模拟模式（mock）：没有板子，什么都不按。',
      boardNoServer: '要等 clawtouch-mcp 能运行，才查得了板子。',
      boardUnknown: '没查到。',
      dryRunOn: '「只看不按」开着：现在不会按任何东西。',
      eyeTitle: '看图模型',
      eyeOptionFollow: '跟随当前对话的模型（默认）',
      eyeOptionMeasured: '{name} · {provider} · 实测命中 {pct}%',
      eyeOptionPlain: '{name} · {provider}',
      eyeOptionUnlisted: '{route}（当前设置，不在能看图的列表里）',
      eyeOptionIncomplete: '（只填了一半，请重新选）',
      eyeHintFollow: '用你和 AI 对话时选的模型看截图，它必须能看图。DeepSeek 默认模型实测大约每 5 次点偏 1 次。',
      eyeHintMeasured: '实测命中 {pct}%。',
      eyeMeasuredNote: '命中率是 2026-11-14 的离线实测：存好的截图，每个模型看 165 次，关思考，模拟 dsh 的图片说明。不代表你配的接口一定一样。',
      eyeHintUnmeasured: '这个模型我们没有实测过，先开「只看不按」试几次再用。',
      eyeModelsLoading: '正在读取 dsh 里的模型…',
      eyeModelsNone: 'dsh 里还没有声明能看图的模型。百炼等服务的配法见说明。',
      eyeModelsFailed: '这些服务商没读到模型列表：{list}',
      eyeGuide: '怎么选看图模型',
      dryRunTitle: '只看不按',
      dryRunHint: '开着时只找位置、报告会点哪里，不按任何东西，不用插板子。第一次在新电脑上用建议开着。',
      advanced: '高级',
      widthLabel: '截图最大宽度（像素）',
      widthHint: '默认 1600，可填 320 到 7680。不是越大越好：模型会自己缩小，宽桌面上的小字反而看不清。',
      commandLabel: 'clawtouch-mcp 的路径',
      commandHint: '默认 clawtouch-mcp（在 PATH 里能找到时）。装在虚拟环境里就填完整路径，比如 C:\\venv\\Scripts\\clawtouch-mcp.exe。',
      legacyTitle: '还有 0.1.x 的旧设置',
      legacyBody: '配置里有 {keys}，这是 0.1.x 直连接口的写法，0.2.0 起不再读取，留着的话看图会直接拒绝。',
      legacyHalfEye: '只填了一半的看图模型（0.1.x 只写 vision.model）也会一起去掉；之后看图跟随当前对话的模型，想用别的模型再在下面选。',
      legacyDrop: '删掉这些旧设置',
      legacyDropping: '正在删除…',
      saveNote: '保存后插件会重新加载：AI 正在进行的点击或输入会被打断。',
      save: '保存',
      saving: '保存中…',
      discard: '放弃修改',
      reloading: '已保存，插件正在用新设置重新加载…',
      saved: '已保存。',
      savedUnconfirmed: '已保存，但重新读到的设置和刚保存的不一致。刷新页面看一下。',
      stale: '设置在别处被改过了，页面已经重新读取。请再改一次。',
      busy: 'AI 正在操作电脑，现在保存会打断它。等这一步做完再保存。',
      overridden: '这一项被更上层的配置（home 级配置或命令行）覆盖了，这里改了也不会生效。',
      invalid: '有一项填得不对：',
      failed: '保存失败。dsh 没能写入配置文件（比如文件里有格式错误），请检查 cordis.patch.yml。',
      feedbackLead: '遇到问题或有建议：',
      feedbackIssue: '到 GitHub 提 issue',
      readme: '插件说明',
      setupTitle: "安装 clawtouch-mcp",
      setupMissing: "插件要靠 clawtouch-mcp（一个 Python 程序）和板子通信，现在它运行不起来。可以用这台电脑上找到的，或者在下面直接装一个。",
      setupOld: "现在用的 clawtouch-mcp 是 {version}，这一版插件要 {required} 或更新（查板子状态要用到）。",
      setupLooking: "正在找这台电脑上的 Python 和 clawtouch-mcp…",
      setupLookFailed: "没找完，点「重新查找」再试。",
      setupFoundManaged: "dsh 目录里已经装好了 clawtouch-mcp {version}。",
      setupFound: "找到 clawtouch-mcp {version}：{path}",
      setupUse: "用这个",
      setupPickPython: "用哪个 Python 安装",
      setupPython: "用 Python {version}（{path}）",
      setupInstall: "安装 clawtouch-mcp {version}",
      setupRescan: "重新查找",
      setupInstallHint: "大约一两分钟。只在点安装时联网：先从 PyPI 下载，不行自动换清华镜像。装进 {root}，不动你系统里的 Python。装好会自动填好路径并保存。",
      setupNoPython: "这台电脑上没找到 Python 3.10 或更新的版本，得先装 Python。Windows 上从 python.org 下载安装包，安装时勾选「Add python.exe to PATH」；装好后点「重新查找」，不用重启 dsh。",
      setupGetPython: "下载 Python",
      setupTooOld: "找到的 Python 版本太旧：{list}",
      setupNoVenv: "找到了 Python，但它缺 venv 模块（Debian、Ubuntu 上要先装 python3-venv）。装好后点「重新查找」。",
      setupPhaseVenv: "正在建独立的 Python 环境…",
      setupPhasePip: "正在下载并安装 clawtouch-mcp…",
      setupPhaseVerify: "正在核对装好的版本…",
      setupDone: "clawtouch-mcp {version} 装好了，正在保存路径…",
      setupDoneMirror: "clawtouch-mcp {version} 装好了（PyPI 不通，用的清华镜像），正在保存路径…",
      setupFailVenv: "建 Python 环境失败。看下面的输出；常见原因是磁盘空间不够或者杀毒软件拦了。",
      setupFailPip: "下载安装失败，PyPI 和清华镜像都没成功。看下面的输出；多半是网络问题，换个网络或开代理后再试。",
      setupFailVerify: "装完核对不对：版本或程序文件不对。再装一次试试。",
      setupFailTimeout: "安装超时（超过 8 分钟），已停止。网络慢的话换个时间再试。",
      setupFailCancelled: "安装被中断了（插件重新加载或 dsh 退出）。再装一次即可，会接着用已经建好的环境。",
      setupFailNoFolder: "dsh 目录里建不了安装文件夹，检查一下那个目录的权限。",
      setupProblemBusy: "AI 正在操作电脑，等这一步做完再装。",
      setupProblemInstalling: "已经在安装了。",
      setupProblemStale: "这台电脑上的 Python 有变化，已重新查找，请再点一次。",
      setupProblemNoVenv: "这个 Python 缺 venv 模块，换一个或先补上。",
      setupProblemFailed: "没能开始安装，刷新页面再试。",
      boardInstalling: "正在安装 clawtouch-mcp，装好后再查板子。",
      setupFailLocked: "另一个 dsh（比如命令行里的）正在往同一个文件夹安装，等它装完再试。",
      setupFailNotOurs: "安装文件夹里已经有一个不是本插件建的 venv（或者它是个链接），为了不误删，没有动它。把它挪走后再装。",
      setupFailOther: "安装出错了，看下面的输出。",
      setupProblemReloading: "插件正在重新加载，等页面刷新后再点。",
      setupFoundNoShot: "找到 clawtouch-mcp {version}（{path}），但它缺截图组件，不能直接用。可以在下面另装一份完整的。",
      installing: "正在安装 clawtouch-mcp，装好后再保存。",
    }

    const en = {
      summary: 'The eye, look-don\'t-press, device status.',
      loading: 'Reading the settings…',
      loadFailed: 'Could not read the settings.',
      retry: 'Retry',
      unavailable: 'This dsh cannot save these settings from the page (no profile editor, or this plugin\'s own row cannot be found). Edit cordis.patch.yml instead; the README shows how.',
      expression: 'A setting is written as a !!js expression, whose value this page cannot show, so the page is read-only. Edit cordis.patch.yml directly.',
      statusTitle: 'Status',
      recheck: 'Check again',
      checking: 'Checking… (nothing is pressed)',
      checkFailed: 'The check did not finish.',
      eyeLabel: 'Eye',
      eyeFollow: 'Follows the conversation\'s model. Whether it can see, and how well, depends on the model picked there.',
      eyeRoute: '{route}',
      imageYes: 'accepts images',
      imageNo: 'dsh says it does not accept images: clicks will be refused; pick another model',
      imageUnknown: 'could not tell whether it accepts images',
      eyeProblemTimeout: 'dsh did not answer within 5 seconds.',
      eyeProblemFailed: 'dsh could not describe this model: the name may be wrong, or the provider is not set up.',
      eyeProblemNoLlm: 'This dsh has no model service.',
      eyeIncomplete: 'Only one of vision.provider and vision.model is set, so looking is refused. Pick the eye again below.',
      legacyStatus: 'The config still has 0.1.x settings, so looking is refused (see below).',
      mcpLabel: 'clawtouch-mcp',
      mcpFound: 'running, version {version}',
      mcpRunning: 'running',
      mcpMissing: 'Cannot run "{command}". Install it below, or if it is already installed, give its full path under Advanced.',
      boardLabel: 'Board',
      boardConnected: 'connected on {port}, firmware {firmware}',
      absOn: 'Absolute pointer: on (one step to the target on the primary screen)',
      absOff: 'Absolute pointer: off ({reason})',
      boardAbsent: 'Not connected.',
      boardSilent: '{port} opened, but the board does not answer. Unplug it, plug it back in, and check again.',
      boardBusy: 'The agent is using the board, so it was not interrupted. Check again once this step is done.',
      boardNoAnswer: 'No answer within 8 seconds.',
      boardOldServer: 'This clawtouch-mcp is too old to report the board. Upgrade to 0.6.1 or later: pip install -U clawtouch-mcp',
      boardOldServerPort: ' (it reports {port})',
      boardMock: 'Mock mode: no board, nothing is pressed.',
      boardNoServer: 'The board can be checked once clawtouch-mcp runs.',
      boardUnknown: 'Could not check.',
      dryRunOn: '"Look, don\'t press" is on: nothing will be pressed.',
      eyeTitle: 'Eye',
      eyeOptionFollow: 'The conversation\'s model (default)',
      eyeOptionMeasured: '{name} · {provider} · {pct}% hits measured',
      eyeOptionPlain: '{name} · {provider}',
      eyeOptionUnlisted: '{route} (current setting, not listed as accepting images)',
      eyeOptionIncomplete: '(half set, pick again)',
      eyeHintFollow: 'The model you talk to looks at the screenshots, so it must accept images. DeepSeek\'s default model misses roughly one click in five in our measurements.',
      eyeHintMeasured: '{pct}% hits measured.',
      eyeMeasuredNote: 'Hit rates are offline measurements from 2026-11-14: saved captures, 165 looks per model, thinking off, dsh\'s image note simulated. Your own endpoint may differ.',
      eyeHintUnmeasured: 'We have not measured this model. Try it with "look, don\'t press" on first.',
      eyeModelsLoading: 'Reading the models configured in dsh…',
      eyeModelsNone: 'No model in dsh declares image input yet. The README shows how to add one (Bailian and others).',
      eyeModelsFailed: 'Could not list the models of: {list}',
      eyeGuide: 'Choosing the eye',
      dryRunTitle: 'Look, don\'t press',
      dryRunHint: 'Finds things and reports where it would click, but presses nothing, and needs no board. Worth turning on for the first run on a new machine.',
      advanced: 'Advanced',
      widthLabel: 'Widest capture (pixels)',
      widthHint: '1600 by default, 320 to 7680. Bigger is not better: the model shrinks the image anyway, and small text on a wide desktop gets lost.',
      commandLabel: 'Path to clawtouch-mcp',
      commandHint: 'clawtouch-mcp by default (when it is on PATH). In a virtual environment, give the full path, e.g. /home/me/venv/bin/clawtouch-mcp.',
      legacyTitle: '0.1.x settings left over',
      legacyBody: 'The config still sets {keys}, the direct-API settings of 0.1.x. 0.2.0 no longer reads them, and while they are there every look is refused.',
      legacyHalfEye: 'The half-set eye goes with them too (0.1.x wrote vision.model alone); the eye then follows the conversation\'s model, and you can pick another below.',
      legacyDrop: 'Remove these old settings',
      legacyDropping: 'Removing…',
      saveNote: 'Saving reloads the plugin: a click or typing the agent is doing right then is cut short.',
      save: 'Save',
      saving: 'Saving…',
      discard: 'Discard changes',
      reloading: 'Saved; the plugin is reloading with the new settings…',
      saved: 'Saved.',
      savedUnconfirmed: 'Saved, but the settings read back differ from what was saved. Reload the page to check.',
      stale: 'The settings were changed elsewhere; the page has read them again. Please make your change again.',
      busy: 'The agent is working the computer right now, and saving would cut it short. Save once this step is done.',
      overridden: 'A layer above the profile (the home patch or a command-line overlay) overrides this, so a change here would have no effect.',
      invalid: 'A field is not acceptable: ',
      failed: 'Saving failed: dsh could not write the profile (a formatting error in it, for one). Check cordis.patch.yml.',
      feedbackLead: 'Problems or suggestions: ',
      feedbackIssue: 'open an issue on GitHub',
      readme: 'README',
      setupTitle: "Install clawtouch-mcp",
      setupMissing: "The plugin reaches the board through clawtouch-mcp, a Python program, and it does not run right now. Use one found on this machine, or install one below.",
      setupOld: "The clawtouch-mcp in use is {version}; this plugin needs {required} or later (for the board check).",
      setupLooking: "Looking for Python and clawtouch-mcp on this machine…",
      setupLookFailed: "The search did not finish; click Look again.",
      setupFoundManaged: "clawtouch-mcp {version} is already installed in dsh's folder.",
      setupFound: "Found clawtouch-mcp {version}: {path}",
      setupUse: "Use this",
      setupPickPython: "Python to install with",
      setupPython: "With Python {version} ({path})",
      setupInstall: "Install clawtouch-mcp {version}",
      setupRescan: "Look again",
      setupInstallHint: "A minute or two. Goes online only when you click Install: from PyPI, and from the Tsinghua mirror if that fails. Installs into {root}, leaving your own Python alone. When done, the path is filled in and saved.",
      setupNoPython: "No Python 3.10 or later was found here, so Python comes first. On Windows, get the installer from python.org and tick \"Add python.exe to PATH\"; then click Look again — no need to restart dsh.",
      setupGetPython: "Download Python",
      setupTooOld: "Python found but too old: {list}",
      setupNoVenv: "Python was found, but without its venv module (on Debian and Ubuntu, install python3-venv). Then click Look again.",
      setupPhaseVenv: "Creating a Python environment of its own…",
      setupPhasePip: "Downloading and installing clawtouch-mcp…",
      setupPhaseVerify: "Checking what was installed…",
      setupDone: "clawtouch-mcp {version} is installed; saving its path…",
      setupDoneMirror: "clawtouch-mcp {version} is installed (PyPI was unreachable, so from the Tsinghua mirror); saving its path…",
      setupFailVenv: "Creating the Python environment failed. See the output below; a full disk or an antivirus program is the usual cause.",
      setupFailPip: "Downloading failed from PyPI and from the Tsinghua mirror. See the output below; it is usually the network — try another network or a proxy.",
      setupFailVerify: "What was installed does not check out (version or program file). Install again.",
      setupFailTimeout: "The install took over 8 minutes and was stopped. Try again when the network is faster.",
      setupFailCancelled: "The install was interrupted (the plugin reloaded or dsh quit). Install again; the environment already made is reused.",
      setupFailNoFolder: "The install folder cannot be created in dsh's folder; check its permissions.",
      setupProblemBusy: "The agent is working the computer; install once this step is done.",
      setupProblemInstalling: "An install is already running.",
      setupProblemStale: "The Pythons on this machine changed; the page looked again. Click once more.",
      setupProblemNoVenv: "That Python has no venv module; pick another or add it.",
      setupProblemFailed: "The install could not start; reload the page and try again.",
      boardInstalling: "clawtouch-mcp is being installed; the board is checked after.",
      setupFailLocked: "Another dsh (one in a terminal, say) is installing into the same folder; try again once it is done.",
      setupFailNotOurs: "The install folder already holds a venv this plugin did not make (or a link), so it was left alone. Move it away and install again.",
      setupFailOther: "The install went wrong; see the output below.",
      setupProblemReloading: "The plugin is reloading; click again once the page has refreshed.",
      setupFoundNoShot: "Found clawtouch-mcp {version} ({path}), but without its screenshot support, so it is not offered. Install a complete copy below.",
      installing: "clawtouch-mcp is being installed; save after it is done.",
    }

    const README = {
      zh: 'https://github.com/tinqiao-oss/clawtouch-mcp/blob/master/adapters/dsh/plugin/README.zh-CN.md',
      en: 'https://github.com/tinqiao-oss/clawtouch-mcp/blob/master/adapters/dsh/plugin/README.md',
    }

    const CSS = `
.ct-page{display:flex;flex-direction:column;gap:18px;font-size:13px;line-height:1.55;color:var(--dsw-alias-label-primary);max-width:760px;}
.ct-section{display:flex;flex-direction:column;gap:8px;}
.ct-title{font-size:14px;font-weight:600;margin:0;}
.ct-heading{display:flex;align-items:center;gap:10px;}
.ct-hint{margin:0;font-size:12px;color:var(--dsw-alias-label-secondary);}
.ct-note{margin:0;font-size:12px;color:var(--dsw-alias-label-tertiary);}
.ct-banner{padding:8px 12px;border-radius:8px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-1);font-size:12px;}
.ct-banner[data-tone="error"]{color:var(--dsw-alias-state-error-primary);}
.ct-banner[data-tone="ok"]{color:var(--dsw-alias-state-success-primary);}
.ct-banner[data-tone="warn"]{color:var(--dsw-alias-state-warn-label);}
.ct-card{display:flex;flex-direction:column;gap:10px;padding:12px 14px;border:.5px solid var(--dsw-alias-border-l3);border-radius:14px;}
.ct-card-head{display:flex;align-items:center;justify-content:space-between;gap:10px;}
.ct-rows{display:flex;flex-direction:column;gap:6px;margin:0;padding:0;list-style:none;}
.ct-row{display:flex;gap:10px;align-items:flex-start;font-size:12.5px;}
.ct-dot{flex:none;width:8px;height:8px;margin-top:6px;border-radius:50%;background:var(--dsw-alias-label-tertiary);}
.ct-dot[data-tone="ok"]{background:var(--dsw-alias-state-success-primary);}
.ct-dot[data-tone="warn"]{background:var(--dsw-alias-state-warn-primary);}
.ct-dot[data-tone="error"]{background:var(--dsw-alias-state-error-primary);}
.ct-row-label{flex:none;width:96px;color:var(--dsw-alias-label-secondary);}
.ct-row-body{display:flex;flex-direction:column;gap:2px;min-width:0;}
.ct-detail{margin:0;font-family:var(--ds-font-family-code,ui-monospace,monospace);font-size:11px;line-height:16px;color:var(--dsw-alias-label-tertiary);overflow-wrap:anywhere;white-space:pre-wrap;}
.ct-switch{position:relative;width:32px;height:18px;border-radius:999px;border:0;padding:0;cursor:pointer;background:var(--dsw-alias-border-l2);flex:none;}
.ct-switch[aria-checked="true"]{background:var(--dsw-alias-brand-primary);}
.ct-switch:disabled{opacity:.5;cursor:default;}
.ct-switch::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--dsw-alias-switch-thumb,#fff);transition:transform .15s ease;}
.ct-switch[aria-checked="true"]::after{transform:translateX(14px);}
.ct-input,.ct-select{box-sizing:border-box;border:1px solid var(--dsw-alias-border-l1);border-radius:8px;background:var(--dsw-alias-bg-layer-1);color:inherit;padding:5px 9px;font:inherit;font-size:12px;outline:none;min-width:0;max-width:100%;}
.ct-select{width:100%;}
.ct-input:focus,.ct-select:focus{border-color:var(--dsw-alias-brand-primary);}
.ct-input[aria-invalid="true"]{border-color:var(--dsw-alias-state-error-primary);}
.ct-field{display:flex;flex-direction:column;gap:4px;}
.ct-field label{font-size:12px;color:var(--dsw-alias-label-secondary);}
.ct-field .ct-input{width:100%;}
.ct-field .ct-input[inputmode="numeric"]{width:120px;}
.ct-advanced{padding:10px 14px;border:.5px solid var(--dsw-alias-border-l3);border-radius:14px;}
.ct-advanced>summary{cursor:pointer;font-size:13px;font-weight:500;}
.ct-advanced[open]>summary{margin-bottom:10px;}
.ct-advanced>.ct-section+.ct-section{margin-top:12px;}
.ct-attn{display:flex;flex-direction:column;gap:6px;padding:10px 14px;border-radius:14px;background:var(--dsw-alias-state-warn-tertiary);background:color-mix(in srgb,var(--dsw-alias-state-warn-primary) 11%,transparent);}
.ct-attn-title{margin:0;font-size:12.5px;font-weight:600;color:var(--dsw-alias-state-warn-label);}
.ct-btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-primary);border-radius:8px;padding:5px 14px;font:inherit;font-size:12px;line-height:18px;cursor:pointer;white-space:nowrap;align-self:flex-start;}
.ct-btn:hover:not(:disabled){background:var(--dsw-alias-interactive-bg-hover);}
.ct-btn:disabled{opacity:.5;cursor:default;}
.ct-btn-primary{background:var(--dsw-alias-button-primary-fill,var(--dsw-alias-brand-primary));border-color:transparent;color:var(--dsw-alias-label-primary-foreground,#fff);}
.ct-btn-primary:hover:not(:disabled){background:var(--dsw-alias-button-primary-fill,var(--dsw-alias-brand-primary));opacity:.9;}
.ct-btn-small{padding:1px 10px;font-size:11.5px;}
.ct-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.ct-footer{display:flex;flex-wrap:wrap;gap:4px 12px;padding-top:12px;border-top:.5px solid var(--dsw-alias-border-l2);font-size:12px;color:var(--dsw-alias-label-tertiary);}
.ct-link{color:var(--dsw-alias-brand-primary);text-decoration:underline;text-underline-offset:2px;}
.ct-link:hover{opacity:.8;}
.ct-btn:focus-visible,.ct-switch:focus-visible,.ct-link:focus-visible,.ct-advanced>summary:focus-visible{outline:var(--dsw-focus-ring-width,2px) solid var(--dsw-focus-ring-color,var(--dsw-alias-brand-primary));outline-offset:1px;}
`

    // ── data ──────────────────────────────────────────────────────────────

    /** The settings the page edits, in the shape a save sends. */
    const settingsOf = (data) => ({
      eye: data.eye?.mode === 'route'
        ? { mode: 'route', provider: data.eye.provider, model: data.eye.model }
        : data.eye?.mode === 'incomplete'
          ? { mode: 'incomplete', provider: data.eye.provider ?? '', model: data.eye.model ?? '' }
          : { mode: 'conversation' },
      dryRun: data.dryRun === true,
      maxWidth: data.maxWidth,
      command: data.command,
    })
    const sameSettings = (a, b) => JSON.stringify(settingsOf(a)) === JSON.stringify(settingsOf(b))
    const routeKey = (provider, model) => JSON.stringify([provider, model])
    const widthOf = (text) => {
      const value = Number(text)
      return /^\s*\d+\s*$/.test(String(text)) && Number.isInteger(value)
        && value >= WIDTH_RANGE[0] && value <= WIDTH_RANGE[1] ? value : undefined
    }
    const commandOf = (text) => {
      const value = String(text).trim()
      // eslint-disable-next-line no-control-regex
      return value && value.length <= 1024 && !/[\u0000-\u001f]/.test(value) ? value : undefined
    }

    /** fetch with a deadline: a request the host never answers must not hold the page busy. */
    async function request(url, { timeout = 15000, ...init } = {}) {
      const deadline = new AbortController()
      const timer = setTimeout(() => deadline.abort(), timeout)
      try {
        const response = await fetch(url, { cache: 'no-store', ...init, signal: deadline.signal,
          headers: { accept: 'application/json', ...(init.headers ?? {}) } })
        // the body is read under the same deadline: headers alone do not finish a request
        const body = await response.json().catch(() => undefined)
        return { ok: response.ok, status: response.status, body }
      } finally {
        clearTimeout(timer)
      }
    }

    async function fetchSettings(timeout) {
      const response = await request(SETTINGS_API, timeout ? { timeout } : {})
      if (!response.ok || response.body === undefined) throw new Error(`HTTP ${response.status}`)
      return response.body
    }

    const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

    // ── pieces ────────────────────────────────────────────────────────────

    function Switch({ checked, disabled, label, onChange }) {
      return h('button', {
        type: 'button', role: 'switch', className: 'ct-switch', 'aria-checked': checked,
        'aria-label': label, disabled, onClick: () => onChange(!checked),
      })
    }

    function Row({ label, tone, children }) {
      return h('li', { className: 'ct-row' },
        h('span', { className: 'ct-dot', 'data-tone': tone, 'aria-hidden': true }),
        h('span', { className: 'ct-row-label' }, label),
        h('div', { className: 'ct-row-body' }, children))
    }

    const Detail = ({ text }) => (text ? h('p', { className: 'ct-detail' }, String(text).slice(0, 600)) : null)

    // ── status ────────────────────────────────────────────────────────────

    const PROBLEM = { timeout: 'eyeProblemTimeout', failed: 'eyeProblemFailed', 'no-llm': 'eyeProblemNoLlm' }

    function eyeRow(eye, t) {
      if (eye === undefined) return null
      if (eye.legacy?.length) {
        return h(Row, { label: t('eyeLabel'), tone: 'error' }, t('legacyStatus'))
      }
      if (eye.mode === 'incomplete') {
        return h(Row, { label: t('eyeLabel'), tone: 'error' }, t('eyeIncomplete'))
      }
      if (eye.mode !== 'route') {
        return h(Row, { label: t('eyeLabel'), tone: 'neutral' }, t('eyeFollow'))
      }
      const route = `${eye.provider} / ${eye.model}`
      const verdict = eye.image === true ? 'imageYes' : eye.image === false ? 'imageNo' : 'imageUnknown'
      const tone = eye.image === true ? 'ok' : eye.image === false ? 'error' : 'warn'
      return h(Row, { label: t('eyeLabel'), tone },
        `${route} · ${t(verdict)}`,
        eye.problem ? h('span', { className: 'ct-hint' }, t(PROBLEM[eye.problem] ?? 'eyeProblemFailed')) : null)
    }

    function mcpRow(mcp, t) {
      if (mcp === undefined) return null
      if (!mcp.found) {
        return h(Row, { label: t('mcpLabel'), tone: 'error' },
          t('mcpMissing', { command: mcp.command }), h(Detail, { text: mcp.detail }))
      }
      return h(Row, { label: t('mcpLabel'), tone: mcp.detail ? 'warn' : 'ok' },
        mcp.version ? t('mcpFound', { version: mcp.version }) : t('mcpRunning'),
        h(Detail, { text: mcp.detail }))
    }

    function boardRow(board, t) {
      if (board === undefined) return null
      const row = (tone, ...children) => h(Row, { label: t('boardLabel'), tone }, ...children)
      switch (board.state) {
        case 'connected': {
          const abs = board.abs
          return row('ok',
            t('boardConnected', { port: board.port, firmware: board.firmware ?? '?' }),
            abs ? h('span', { className: 'ct-hint' },
              abs.enabled ? t('absOn') : t('absOff', { reason: abs.reason ?? '' })) : null)
        }
        case 'absent': return row('error', t('boardAbsent'), h(Detail, { text: board.detail }))
        case 'silent': return row('error', t('boardSilent', { port: board.port }), h(Detail, { text: board.detail }))
        case 'busy': return row('neutral', t('boardBusy'))
        case 'no-answer': return row('warn', t('boardNoAnswer'))
        case 'old-server': return row('warn', t('boardOldServer') + (board.port ? t('boardOldServerPort', { port: board.port }) : ''))
        case 'mock': return row('warn', t('boardMock'))
        case 'no-server': return row('neutral', t('boardNoServer'))
        case 'installing': return row('neutral', t('boardInstalling'))
        default: return row('warn', t('boardUnknown'), h(Detail, { text: board.detail }))
      }
    }

    /** What the eye and the device look like now. Checked on open and on request; never presses. */
    function StatusCard({ t, dryRun, generation, onStatus }) {
      const [state, setState] = useState({ phase: 'checking' })
      const alive = useRef(true)
      const running = useRef(false)
      // asked again while a check runs (a save landed meanwhile): that answer
      // may describe the plugin before the reload, so it is dropped and the
      // check runs once more
      const again = useRef(false)
      useEffect(() => () => { alive.current = false }, [])
      const check = useCallback(async () => {
        if (running.current) { again.current = true; return }
        running.current = true
        setState((s) => ({ phase: 'checking', data: s.data }))
        let next
        try {
          const response = await request(STATUS_API, { method: 'POST', timeout: 20000 })
          next = !response.ok || response.body === undefined ? { phase: 'error' } : { phase: 'done', data: response.body }
        } catch {
          next = { phase: 'error' }
        } finally {
          running.current = false
        }
        if (!alive.current) return
        if (again.current) {
          again.current = false
          check()
          return
        }
        setState(next)
        if (next.phase === 'done') onStatus?.(next.data)
      }, [])
      // again after every save: the plugin reloaded, possibly with another command or eye
      useEffect(() => { check() }, [check, generation])
      const data = state.data
      return h('section', { className: 'ct-card', 'aria-busy': state.phase === 'checking' },
        h('div', { className: 'ct-card-head' },
          h('h3', { className: 'ct-title' }, t('statusTitle')),
          h('button', { type: 'button', className: 'ct-btn ct-btn-small', disabled: state.phase === 'checking',
            onClick: check }, t('recheck'))),
        state.phase === 'checking' ? h('p', { className: 'ct-note', role: 'status' }, t('checking')) : null,
        state.phase === 'error' ? h('p', { className: 'ct-note', role: 'status' }, t('checkFailed')) : null,
        data ? h('ul', { className: 'ct-rows' },
          eyeRow(data.eye, t), mcpRow(data.mcp, t), boardRow(data.board, t)) : null,
        dryRun ? h('p', { className: 'ct-hint' }, t('dryRunOn')) : null)
    }

    // ── settings ──────────────────────────────────────────────────────────

    /** dsh's routes that declare image input, read once per page. */
    function useModels() {
      const [models, setModels] = useState({ phase: 'loading' })
      useEffect(() => {
        let alive = true
        request(MODELS_API, { timeout: 20000 })
          .then((r) => { if (alive) setModels(r.ok && r.body ? { phase: 'ready', ...r.body } : { phase: 'error' }) })
          .catch(() => { if (alive) setModels({ phase: 'error' }) })
        return () => { alive = false }
      }, [])
      return models
    }

    function EyeSection({ draft, models, t, disabled, update, readme }) {
      const routes = models.phase === 'ready' ? models.routes ?? [] : []
      const eye = draft.eye
      const listed = eye.mode === 'route' && routes.some((r) => r.provider === eye.provider && r.model === eye.model)
      const value = eye.mode === 'route' ? routeKey(eye.provider, eye.model) : eye.mode === 'incomplete' ? 'incomplete' : 'conversation'
      const options = [h('option', { key: 'conversation', value: 'conversation' }, t('eyeOptionFollow'))]
      if (eye.mode === 'incomplete') {
        options.push(h('option', { key: 'incomplete', value: 'incomplete', disabled: true }, t('eyeOptionIncomplete')))
      }
      if (eye.mode === 'route' && !listed) {
        options.push(h('option', { key: 'current', value }, t('eyeOptionUnlisted', { route: `${eye.provider} / ${eye.model}` })))
      }
      for (const r of routes) {
        const pct = MEASURED[r.model]
        options.push(h('option', { key: routeKey(r.provider, r.model), value: routeKey(r.provider, r.model) },
          pct ? t('eyeOptionMeasured', { name: r.name, provider: r.providerName, pct })
            : t('eyeOptionPlain', { name: r.name, provider: r.providerName })))
      }
      const choose = (event) => {
        const v = event.target.value
        if (v === 'conversation') return update({ eye: { mode: 'conversation' } })
        try {
          const [provider, model] = JSON.parse(v)
          update({ eye: { mode: 'route', provider, model } })
        } catch { /* the disabled placeholder */ }
      }
      let hint
      if (eye.mode === 'conversation') hint = t('eyeHintFollow')
      else if (eye.mode === 'route') hint = MEASURED[eye.model] ? t('eyeHintMeasured', { pct: MEASURED[eye.model] }) : t('eyeHintUnmeasured')
      else hint = t('eyeIncomplete')
      return h('section', { className: 'ct-section' },
        h('h3', { className: 'ct-title' }, t('eyeTitle')),
        h('select', { className: 'ct-select', value, disabled, 'aria-label': t('eyeTitle'), onChange: choose }, options),
        h('p', { className: 'ct-hint' }, hint),
        routes.some((r) => MEASURED[r.model]) || MEASURED[eye.model] ? h('p', { className: 'ct-note' }, t('eyeMeasuredNote')) : null,
        models.phase === 'loading' ? h('p', { className: 'ct-note' }, t('eyeModelsLoading')) : null,
        models.phase === 'ready' && routes.length === 0 ? h('p', { className: 'ct-note' }, t('eyeModelsNone')) : null,
        models.phase === 'ready' && models.failed?.length
          ? h('p', { className: 'ct-note' }, t('eyeModelsFailed', { list: models.failed.join(', ') })) : null,
        h('p', { className: 'ct-note' },
          h('a', { className: 'ct-link', href: readme, target: '_blank', rel: 'noreferrer' }, t('eyeGuide'))))
    }

    function LegacyNotice({ keys, t, busy, disabled, halfEye, onDrop }) {
      return h('section', { className: 'ct-attn', role: 'alert' },
        h('p', { className: 'ct-attn-title' }, t('legacyTitle')),
        h('p', { className: 'ct-hint' }, t('legacyBody', { keys: keys.map((k) => `vision.${k}`).join(', ') })),
        halfEye ? h('p', { className: 'ct-hint' }, t('legacyHalfEye')) : null,
        disabled ? null : h('button', { type: 'button', className: 'ct-btn ct-btn-small', disabled: busy, onClick: onDrop },
          busy ? t('legacyDropping') : t('legacyDrop')))
    }

    /** The settings as the host has them, and a save that waits for the reloaded plugin to read them back. */
    function useSettings(t, onAdopt, onSaved) {
      const [state, setState] = useState({ phase: 'loading' })
      const [busy, setBusy] = useState(false)
      const [banner, setBanner] = useState(undefined)
      const alive = useRef(true)
      // a second click before the page re-renders as busy must not send a second save
      const saving = useRef(false)
      useEffect(() => () => { alive.current = false }, [])

      const adopt = useCallback((data, options) => {
        setState({ phase: 'ready', data })
        onAdopt?.(data, options)
      }, [onAdopt])
      const load = useCallback(async () => {
        setState((s) => (s.phase === 'ready' ? s : { phase: 'loading' }))
        try {
          const data = await fetchSettings()
          if (alive.current) adopt(data)
        } catch {
          if (alive.current) setState({ phase: 'error' })
        }
      }, [adopt])
      useEffect(() => { load() }, [load])

      const save = async (next, { dropLegacy = false, keepDraft = dropLegacy, takeCommand = false } = {}) => {
        if (saving.current || state.phase !== 'ready') return
        saving.current = true
        setBusy(true)
        setBanner(undefined)
        try {
          let result
          try {
            const response = await request(SETTINGS_API, {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ revision: state.data.revision, ...next, dropLegacy }),
            })
            result = response.body ?? { ok: false, error: 'failed', message: `HTTP ${response.status}` }
          } catch (error) {
            result = { ok: false, error: 'failed', message: String(error?.message ?? error) }
          }
          if (!alive.current) return
          if (result.ok) {
            // The plugin reloads with the new config; its routes come back with it.
            setBanner({ tone: 'ok', text: t('reloading') })
            const landed = (fresh) => sameSettings(fresh, next) && (!dropLegacy || (fresh.legacy ?? []).length === 0)
            let fresh
            // a bounded wait in all: the form stays disabled until it ends
            const until = Date.now() + READ_BACK_MS
            while (alive.current && until - Date.now() > 400) {
              await wait(400)
              const left = until - Date.now()
              if (left <= 0) break
              try {
                fresh = await fetchSettings(left)
                if (landed(fresh)) break
              } catch { /* still reloading */ }
            }
            if (!alive.current) return
            // removing the old keys alone must not throw away an unsaved draft
            if (fresh !== undefined) adopt(fresh, { keepDraft, takeCommand })
            setBanner(fresh !== undefined && landed(fresh)
              ? { tone: 'ok', text: t('saved') }
              : { tone: 'error', text: t('savedUnconfirmed') })
            onSaved?.()
          } else if (result.error === 'installing') {
            setBanner({ tone: 'error', text: t('installing') })
          } else if (result.error === 'busy') {
            setBanner({ tone: 'error', text: t('busy') })
          } else if (result.error === 'stale' || result.error === 'expression') {
            // the profile changed under the page: read it again
            await load()
            if (alive.current) setBanner({ tone: 'error', text: t(result.error) })
          } else if (result.error === 'overridden') {
            setBanner({ tone: 'error', text: t('overridden') })
          } else if (result.error === 'invalid') {
            setBanner({ tone: 'error', text: t('invalid') + (result.field ?? '') })
          } else if (result.error === 'unavailable') {
            setBanner({ tone: 'error', text: t('unavailable') })
          } else {
            setBanner({ tone: 'error', text: t('failed') })
          }
        } finally {
          saving.current = false
          if (alive.current) setBusy(false)
        }
      }
      /** Whether a save would start now (none under way, the settings read). */
      const canSave = () => !saving.current && state.phase === 'ready'
      return { state, busy, banner, setBanner, load, save, canSave }
    }

    // ── installing clawtouch-mcp ──────────────────────────────────────────

    /** As lib/installer.js compares (a PEP 440 subset): pre-releases and dev builds before
     *  the release, post releases after it. A test keeps the two in step. */
    const versionCmp = (a, b) => {
      const parse = (v) => {
        const m = /^\s*v?(\d+(?:\.\d+)*)(.*)$/i.exec(String(v))
        if (!m) return { release: [0], rank: 0 }
        const rest = m[2].toLowerCase()
        const rank = !rest || rest.startsWith('+') ? 0 : /^[.-]?post/.test(rest) ? 1 : -1
        return { release: m[1].split('.').map(Number), rank }
      }
      const x = parse(a)
      const y = parse(b)
      for (let i = 0; i < Math.max(x.release.length, y.release.length); i++) {
        const d = (x.release[i] ?? 0) - (y.release[i] ?? 0)
        if (d) return d < 0 ? -1 : 1
      }
      return Math.sign(x.rank - y.rank)
    }

    /** Whether the page should offer to install or upgrade clawtouch-mcp, given a status answer. */
    const needsSetup = (status) => Boolean(status) && (
      status.mcp?.found === false
      || status.board?.state === 'old-server'
      || status.board?.state === 'installing'
      || (status.mcp?.version !== undefined && versionCmp(status.mcp.version, REQUIRED_MCP) < 0))

    const FAIL = {
      venv: 'setupFailVenv', pip: 'setupFailPip', verify: 'setupFailVerify', timeout: 'setupFailTimeout',
      cancelled: 'setupFailCancelled', 'no-folder': 'setupFailNoFolder', locked: 'setupFailLocked',
      'not-ours': 'setupFailNotOurs', failed: 'setupFailOther',
    }
    const SETUP_PROBLEM = {
      busy: 'setupProblemBusy', installing: 'setupProblemInstalling', stale: 'setupProblemStale',
      'no-venv': 'setupProblemNoVenv', reloading: 'setupProblemReloading',
    }
    const PHASE = { venv: 'setupPhaseVenv', pip: 'setupPhasePip', verify: 'setupPhaseVerify' }

    /**
     * Find clawtouch-mcp on this machine, or install it into a venv of the plugin's own.
     * Shown only when the status says the server cannot run or is too old.
     */
    function SetupCard({ t, status, editable, onUse }) {
      const [look, setLook] = useState({ phase: 'loading' })
      const [job, setJob] = useState(null)
      const [choice, setChoice] = useState(undefined)
      const [problem, setProblem] = useState(undefined)
      const alive = useRef(true)
      const used = useRef(null)
      useEffect(() => () => { alive.current = false }, [])

      const find = useCallback(async () => {
        setLook({ phase: 'loading' })
        setProblem(undefined)
        try {
          // runs each Python it finds once; on a slow disk that takes a while
          const r = await request(SETUP_API, { timeout: 60000 })
          if (!alive.current) return
          if (!r.ok || !r.body) { setLook({ phase: 'error' }); return }
          setLook({ phase: 'ready', data: r.body })
          if (r.body.job) setJob(r.body.job)
          setChoice(r.body.pythons.find((p) => p.venv)?.id)
        } catch {
          if (alive.current) setLook({ phase: 'error' })
        }
      }, [])
      useEffect(() => { find() }, [find])

      // follow a running install until it ends
      const running = Boolean(job) && job.phase !== 'done' && job.phase !== 'failed'
      useEffect(() => {
        if (!running) return undefined
        let stop = false
        ;(async () => {
          while (!stop && alive.current) {
            await wait(1000)
            try {
              const r = await request(INSTALL_API, { timeout: 10000 })
              if (!alive.current || !r.ok) continue
              if (!r.body?.job) {
                // the plugin reloaded under the install (which cancelled it): say so, stop following
                setJob((j) => ({ ...(j ?? {}), phase: 'failed', error: 'cancelled', lines: j?.lines ?? [] }))
                break
              }
              setJob(r.body.job)
              if (r.body.job.phase === 'done' || r.body.job.phase === 'failed') break
            } catch { /* try again */ }
          }
        })()
        return () => { stop = true }
      }, [running])

      // installed: make it the command, once
      useEffect(() => {
        // onUse says whether a save started; while another is under way it did not, and
        // this runs again on the next render until one does
        if (job?.phase === 'done' && job.path && used.current !== job.path && onUse(job.path)) {
          used.current = job.path
        }
      }, [job, onUse])

      const install = async () => {
        setProblem(undefined)
        try {
          const r = await request(INSTALL_API, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ python: choice, generation: look.data?.generation }),
          })
          if (!alive.current) return
          if (r.status === 202 && r.body?.job) setJob(r.body.job)
          else {
            setProblem(r.body?.error ?? 'failed')
            if (r.body?.error === 'stale') find()
          }
        } catch {
          if (alive.current) setProblem('failed')
        }
      }

      const old = status.board?.state === 'old-server'
        || (status.mcp?.found && status.mcp.version !== undefined && versionCmp(status.mcp.version, REQUIRED_MCP) < 0)
      const parts = [
        h('h3', { className: 'ct-title', key: 'title' }, t('setupTitle')),
        h('p', { className: 'ct-hint', key: 'intro' }, old
          ? t('setupOld', { version: status.mcp?.version ?? '?', required: REQUIRED_MCP })
          : t('setupMissing')),
      ]
      const data = look.data
      if (look.phase === 'loading') parts.push(h('p', { className: 'ct-note', key: 'looking', role: 'status' }, t('setupLooking')))
      if (look.phase === 'error') {
        parts.push(h('p', { className: 'ct-note', key: 'lookfail' }, t('setupLookFailed')))
        parts.push(h('div', { className: 'ct-actions', key: 'lookretry' },
          h('button', { type: 'button', className: 'ct-btn', onClick: find }, t('setupRescan'))))
      }
      if (data) {
        const current = status.mcp?.command
        const usable = [
          ...(data.managed?.version && versionCmp(data.managed.version, REQUIRED_MCP) >= 0 && data.managed.screenshot !== false
            ? [{ path: data.managed.path, version: data.managed.version, managed: true }] : []),
          ...data.found.filter((f) => versionCmp(f.version, REQUIRED_MCP) >= 0 && f.screenshot !== false),
        ].filter((f) => f.path !== current)
        // found, recent enough, but without what screenshots need: not offered, but said
        for (const f of data.found.filter((x) => versionCmp(x.version, REQUIRED_MCP) >= 0 && x.screenshot === false)) {
          parts.push(h('p', { className: 'ct-note', key: `noshot-${f.path}` }, t('setupFoundNoShot', { version: f.version, path: f.path })))
        }
        for (const f of usable) {
          parts.push(h('div', { className: 'ct-actions', key: `use-${f.path}` },
            h('span', { className: 'ct-hint' }, f.managed
              ? t('setupFoundManaged', { version: f.version })
              : t('setupFound', { version: f.version, path: f.path })),
            h('button', { type: 'button', className: 'ct-btn ct-btn-small', disabled: !editable || running,
              onClick: () => onUse(f.path) }, t('setupUse'))))
        }
        const pythons = data.pythons.filter((p) => p.venv)
        if (pythons.length > 1) {
          parts.push(h('select', { className: 'ct-select', key: 'pick', value: String(choice ?? ''), 'aria-label': t('setupPickPython'),
            disabled: running, onChange: (e) => setChoice(Number(e.target.value)) },
          pythons.map((p) => h('option', { key: p.id, value: String(p.id) }, t('setupPython', { version: p.version, path: p.executable })))))
        } else if (pythons.length === 1) {
          parts.push(h('p', { className: 'ct-note', key: 'py' }, t('setupPython', { version: pythons[0].version, path: pythons[0].executable })))
        }
        if (pythons.length) {
          parts.push(h('div', { className: 'ct-actions', key: 'install' },
            h('button', { type: 'button', className: 'ct-btn ct-btn-primary', disabled: !editable || running || choice === undefined,
              onClick: install }, t('setupInstall', { version: REQUIRED_MCP })),
            h('button', { type: 'button', className: 'ct-btn', disabled: running, onClick: find }, t('setupRescan'))))
          parts.push(h('p', { className: 'ct-note', key: 'how' }, t('setupInstallHint', { root: data.root })))
        } else {
          if (data.pythons.length) parts.push(h('p', { className: 'ct-hint', key: 'novenv' }, t('setupNoVenv')))
          else parts.push(h('p', { className: 'ct-hint', key: 'nopy' }, t('setupNoPython')))
          if (data.tooOld?.length) {
            parts.push(h('p', { className: 'ct-note', key: 'old' },
              t('setupTooOld', { list: data.tooOld.map((p) => `${p.version} (${p.executable})`).join(', ') })))
          }
          parts.push(h('div', { className: 'ct-actions', key: 'getpy' },
            h('a', { className: 'ct-link', href: 'https://www.python.org/downloads/', target: '_blank', rel: 'noreferrer' },
              t('setupGetPython')),
            h('button', { type: 'button', className: 'ct-btn', onClick: find }, t('setupRescan'))))
        }
      }
      if (problem) {
        parts.push(h('div', { className: 'ct-banner', 'data-tone': 'error', key: 'problem', role: 'status' },
          t(SETUP_PROBLEM[problem] ?? 'setupProblemFailed')))
      }
      if (job) {
        const text = job.phase === 'done'
          ? t(job.source === 'tuna' ? 'setupDoneMirror' : 'setupDone', { version: job.version })
          : job.phase === 'failed' ? t(FAIL[job.error] ?? 'setupFailPip') : t(PHASE[job.phase] ?? 'setupPhasePip')
        parts.push(h('div', { className: 'ct-banner', key: 'job', role: 'status',
          'data-tone': job.phase === 'done' ? 'ok' : job.phase === 'failed' ? 'error' : undefined }, text))
        if (job.phase !== 'done' && job.lines?.length) {
          parts.push(h('p', { className: 'ct-detail', key: 'lines' }, job.lines.slice(-8).join('\n')))
        }
      }
      return h('section', { className: 'ct-card ct-setup', 'aria-busy': running || look.phase === 'loading' }, ...parts)
    }

    /** The package's own page in the Plugins page. */
    function SettingsForm({ t, locale }) {
      const [draft, setDraft] = useState(undefined)
      const [width, setWidth] = useState(String(DEFAULT_WIDTH))
      const [command, setCommand] = useState(DEFAULT_COMMAND)
      const [generation, setGeneration] = useState(0)
      const [status, setStatus] = useState(undefined)
      const onStatus = useCallback((data) => setStatus(data), [])
      const onAdopt = useCallback((data, options) => {
        const settings = settingsOf(data)
        if (options?.keepDraft) {
          // the draft stays, but a half-set eye was just replaced on disk
          // (removing the 0.1.x settings) and must not linger in it
          setDraft((d) => (d?.eye.mode === 'incomplete' ? { ...d, eye: settings.eye } : d))
          // a clawtouch-mcp just installed or picked on the page: its path is the new command
          if (options.takeCommand) {
            setDraft((d) => (d ? { ...d, command: settings.command } : d))
            setCommand(settings.command)
          }
          return
        }
        setDraft(settings)
        setWidth(String(settings.maxWidth))
        setCommand(settings.command)
      }, [])
      const onSaved = useCallback(() => setGeneration((n) => n + 1), [])
      const { state, busy, banner, setBanner, load, save, canSave } = useSettings(t, onAdopt, onSaved)
      const models = useModels()
      const readme = README[locale] ?? README.en

      if (state.phase === 'error') {
        return h('div', { className: 'ct-page' },
          h('div', { className: 'ct-banner', 'data-tone': 'error' }, t('loadFailed')),
          h('div', { className: 'ct-actions' }, h('button', { type: 'button', className: 'ct-btn', onClick: load }, t('retry'))))
      }
      if (state.phase === 'loading' || draft === undefined) {
        return h('p', { className: 'ct-note' }, t('loading'))
      }

      const { data } = state
      const editable = data.editable === true
      const disabled = !editable || busy
      const saved = settingsOf(data)
      const maxWidth = widthOf(width)
      const cmd = commandOf(command)
      const next = { ...draft, maxWidth: maxWidth ?? draft.maxWidth, command: cmd ?? draft.command }
      const valid = maxWidth !== undefined && cmd !== undefined && next.eye.mode !== 'incomplete'
      const dirty = JSON.stringify(next) !== JSON.stringify(saved)
      const update = (patch) => { setDraft({ ...draft, ...patch }); setBanner(undefined) }
      const discard = () => {
        setDraft(saved)
        setWidth(String(saved.maxWidth))
        setCommand(saved.command)
        setBanner(undefined)
      }
      const legacy = data.legacy ?? []

      return h('div', { className: 'ct-page' },
        h(StatusCard, { t, dryRun: saved.dryRun, generation, onStatus }),
        needsSetup(status) ? h(SetupCard, { t, status, editable,
          // the saved settings with the new path: an unsaved draft stays a draft
          onUse: (path) => {
            if (!canSave()) return false
            save({ ...saved, command: path }, { keepDraft: true, takeCommand: true })
            return true
          } }) : null,
        editable ? null : h('div', { className: 'ct-banner' },
          t(data.readOnly === 'expression' ? 'expression' : 'unavailable')),
        legacy.length ? h(LegacyNotice, { keys: legacy, t, busy, disabled: !editable,
          halfEye: saved.eye.mode === 'incomplete',
          // removes only the old keys: the saved settings, not the unsaved draft, go with
          // it — except a lone vision.model, the 0.1.x way of naming the model, which
          // goes too (the eye then follows the conversation, as with nothing set)
          onDrop: () => save(saved.eye.mode === 'incomplete' ? { ...saved, eye: { mode: 'conversation' } } : saved,
            { dropLegacy: true }) }) : null,
        h(EyeSection, { draft, models, t, disabled, update, readme }),
        h('section', { className: 'ct-section' },
          h('div', { className: 'ct-heading' },
            h('h3', { className: 'ct-title' }, t('dryRunTitle')),
            h(Switch, { checked: draft.dryRun, disabled, label: t('dryRunTitle'),
              onChange: (on) => update({ dryRun: on }) })),
          h('p', { className: 'ct-hint' }, t('dryRunHint'))),
        h('details', { className: 'ct-advanced' },
          h('summary', null, t('advanced')),
          h('div', { className: 'ct-section ct-field' },
            h('label', { htmlFor: 'ct-width' }, t('widthLabel')),
            h('input', { id: 'ct-width', className: 'ct-input', inputMode: 'numeric', value: width, disabled,
              'aria-invalid': maxWidth === undefined, onChange: (e) => { setWidth(e.target.value); setBanner(undefined) } }),
            h('p', { className: 'ct-hint' }, t('widthHint'))),
          h('div', { className: 'ct-section ct-field' },
            h('label', { htmlFor: 'ct-command' }, t('commandLabel')),
            h('input', { id: 'ct-command', className: 'ct-input', value: command, disabled, spellCheck: false,
              'aria-invalid': cmd === undefined, onChange: (e) => { setCommand(e.target.value); setBanner(undefined) } }),
            h('p', { className: 'ct-hint' }, t('commandHint')))),
        banner ? h('div', { className: 'ct-banner', 'data-tone': banner.tone, role: 'status' }, banner.text) : null,
        editable ? h('div', { className: 'ct-section' },
          h('div', { className: 'ct-actions' },
            h('button', { type: 'button', className: 'ct-btn ct-btn-primary', disabled: busy || !dirty || !valid,
              onClick: () => save(next) }, busy ? t('saving') : t('save')),
            h('button', { type: 'button', className: 'ct-btn', disabled: busy || !dirty, onClick: discard }, t('discard'))),
          dirty ? h('p', { className: 'ct-note' }, t('saveNote')) : null) : null,
        h('footer', { className: 'ct-footer' },
          h('span', null, `dsh-clawtouch ${VERSION}`),
          h('span', null, t('feedbackLead'),
            h('a', { className: 'ct-link', href: ISSUES, target: '_blank', rel: 'noreferrer' }, t('feedbackIssue'))),
          h('a', { className: 'ct-link', href: readme, target: '_blank', rel: 'noreferrer' }, t('readme'))))
    }

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        ctx.effect(() => ctx.locale.register(NS, { zh, en }), 'dsh-clawtouch: dictionaries')
        ctx.effect(() => {
          const tag = document.createElement('style')
          tag.dataset.plugin = 'dsh-clawtouch'
          tag.dataset.pluginCss = 'dsh-clawtouch/settings.css'
          tag.textContent = CSS
          document.head.appendChild(tag)
          return () => tag.remove()
        }, 'dsh-clawtouch: styles')
        const language = () => {
          try {
            const tag = String(ctx.locale.current?.() ?? ctx.locale.locale ?? document.documentElement.lang ?? '')
            return tag.toLowerCase().startsWith('zh') ? 'zh' : 'en'
          } catch {
            return 'en'
          }
        }
        function SettingsPage(props) {
          if (props.view === 'summary') return props.t('summary')
          return h(SettingsForm, { ...props, locale: language() })
        }
        ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({
          name: 'plugins.bundle.config',
          key: PACKAGE,
          locale: NS,
        }, SettingsPage))
      },
    }
  },
})
