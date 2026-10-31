---
name: computer-use
description: 用 computer_use 工具自主操控 Windows 桌面——截屏、点击、输入、开应用、管窗口、按元素操作、后台凝视、虚拟桌面。要打开或操作本地应用、Windows 设置、文件选择器、用户指定的本地浏览器会话，而没有 API / MCP / 技能可走时用。网页内的活优先 browser-use，其中要登录态或有外部后果的站内任务转 huashu-chrome。原则：能走 API 和命令行就不动鼠标。
---

# computer-use — 桌面操控

## 0 输出纪律（最高优先级）
**除工具调用外一个字符都不要多写。** 不写推理、计划、"我先看看"、复述规则、解释步骤、
总结、寒暄。每次回复直接就是工具调用；一次任务只在最后一句交代结果（≤30 字）。

**思维链按任务类型分流**：

| 任务 | 思维链 |
|---|---|
| 单步、能一眼验证（开应用/点一下/截一张/发一条） | **≤1 行（≤20 字）**，想完就调工具 |
| 要先规划：画图/排版/批量录入/跨窗口搬运/找规律/陌生软件 | 允许**一次 5~15 行**计划（todo 或 eval 注释），之后每步 ≤1 行 |
| 连续 2 次同类失败，或用户说"仔细想" | 停下来写诊断，再动手 |

判据：**下一步要选 ≥3 个坐标或 ≥3 个动作的先后 → 先想；做完能一眼验证 → 不想。**
想的时候不要复述规则、复演上一步、预演失败、自问自答。
要用户介入（密码/授权/界面坏了）：先 `focus_cc` 再一句话求助。

## 1 用工具，别用 shell 拼命令
调 `computer_use`（action + 扁平参数）；CLI 只作降级/调试。
坐标 0-1000 归一化；只有 `region` / `element_at` 吃物理像素。
action 全量见工具 description；参数拿不准用 `argv:["click","500","500"]`。

## 2 两个平面怎么选
网页内的活优先 `browser_use`（吃树，不靠截图估坐标）；不确定哪些窗口是浏览器就
`hybrid_programs`。本地应用、系统设置、文件选择器、任务栏、用户要"我开着的浏览器"→ 用本工具。
**要登录态、或有外部后果（发布/下单/提交）的站内任务，转 `huashu-chrome` 技能**——
那两个浏览器不带用户的登录态，硬做只会卡在登录墙。
**一个工作流内平面固定，不混用。**

## 3 三条硬规矩
1. **看一眼只花一个来回**：正常用 `shot`（直接回图、不落盘）；要留证据才给 `out`；
   只看布局不读小字时给 `long:800`（图像 token 省一半）。
2. **等不要用连拍截图代替**：等外部事件先 `watch_start` → `watch_recv`（后台排队、
   等待零 token）；非等不可才 `wait`，≤2 秒、一轮 ≤2 次。
3. **不做任务之外的事**：不清理截图、不关遗留标签、不列目录、不跑 -h、不读技能文档。

## 4 开应用 / 进窗口
`launch` 用中文显示名；名字不确定先 `apps 关键词`，别反复 launch。
进窗口两步：`focus 名字` → `shot window=名字`，别先 windows/全屏 shot 探路。
`accepted_unverified` 不是错误：**不要再 launch**，等窗口自己出来。

## 5 元素级（比截图估坐标可靠）
`state_capture` 给可交互元素编号 `e12`；`element e12 --invoke`（或 set_value/toggle/select/
expand/scroll_into_view）走 UI Automation，不依赖坐标。
- ref 只对**本次进程**的快照有效：界面变了或换调用就重新 state_capture。
- 元素拒绝某模式：先 `element e12 --get patterns` 看支持什么，最后才退回 `--click`。
- 自绘界面没有可访问性树（CU_ELEMENT）→ 退回 shot 看图。

## 6 多步执行
`batch{commands}` 一次跑多条 CLI 行，遇错默认停（`continueOnError` 可继续）。
`eval{code}` 在服务端跑一小段 Python（可直接 `cu.click(...)`、循环、判断、读文件）——
**一次调用干完一串事，别每步都模型往返**。超时 CU_EVAL_TIMEOUT，用 server_restart 回收。
`eval`/`spawn` 是逃生舱：**不许用来绕过 confirm 闸**，也不许做任务外的事。

## 7 虚拟桌面
`desktop_current` 拿真实 GUID；`desktop_switch{direction:"left|right"}` 走热键，最可靠。
`desktop_list` 可能 `available:false`（内部 COM 多数机器拿不到），**不许编造列表**。
`target:"pip"` 是豆包沙箱概念，本机不支持。新建/关闭桌面要 `confirm:true`。

## 8 对用户开口前先看他在哪
- 用户在**桌面/终端**前 → 先 `focus_cc`，否则他看不见。
- 用户在看 **DSH Web GUI / 浏览器** → **不要** focus_cc（会把无关终端抢到前面），直接说话。
- 正在 `watch` 时不要抢焦点。

## 9 开工前同意 / 收尾
默认先问用户是否接管键鼠；用户已明确让你操控的，直接做。
收尾只写一句话（做了什么/还剩什么/要不要接手）；留证据贴路径；按 §8 决定是否 focus_cc。

## 10 status 怎么读
`invalid_action` 改参数；`state_changed` 重新观察（别用旧坐标/旧 ref）；
`blocked` 是系统限制或要确认（UIPI/锁屏→请用户接管；confirm 闸→说清后果再确认）；
`timeout` 换策略或 server_restart。错误码 `[CU_XXX]` 稳定，`codes` 能列全部。
登录/验证码/MFA/选文件：用 ask_user_question 请用户接管，不要硬试或绕过。
