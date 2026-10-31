# UIA 常驻子进程（元素级能力）—— 对齐豆包 aha_cua 的 state.capture / visual_tree.* / app.*
#
# 为什么必须是「常驻」的：
#   ref（e12 这种编号）要能跨调用存活，就必须让 .NET 的 AutomationElement 对象
#   一直活在某个进程里。每次调用起一个新 PowerShell 的话，上一轮的元素对象已经没了，
#   ref 就成了空号。所以这里是一个 stdin/stdout 上的 JSON 行协议服务。
#
# 协议：一行一个 JSON 请求，一行一个 JSON 响应。
#   请求 {"id":N,"op":"snapshot","window":"记事本","depth":12,"limit":400,"maxms":15000}
#   响应 {"id":N,"ok":true,"gen":1,"refs":[...],"truncated":false}
# 响应里 ok=false 时带 code（CU_*）与 message，Python 侧直接转成 CuError。
#
# 三条性能与安全的纪律（都是 UIA 的经典坑）：
#   1. **绝不无条件枚举**。取前台窗口走 FromHandle(GetForegroundWindow())，
#      不要"先枚举全部顶层窗口再逐个比对"，那在 UIA 上是很贵的跨进程调用。
#   2. **模式（pattern）按需探测**。每个节点探 8 个 pattern 就是 8 次跨进程调用；
#      只对"值得编号"的节点探，容器节点一律跳过。
#   3. **有墙钟预算**。UIA 调用会在目标进程忙的时候长时间阻塞（且不可中断），
#      所以遍历必须带 maxms 上限，到点就停下并标 truncated——宁可少给，
#      也不能把整个常驻服务挂死。
#
# 只做「问系统 UI 是怎么回事」和「对元素执行模式动作」。
# **需要真鼠标的兜底点击不在这里做**：这里只回一个可点中心点，
# 由 Python 侧走它自己那套 click（带静默窗、带修饰键复位），不重复造一套注入。

[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

# WindowsBase 是给 System.Windows.Point 用的（FromPoint 要它）；不加就报
# "Cannot find type [System.Windows.Point]"，而且是在**运行到那一步**才炸。
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase

# 取前台窗口句柄：0 次 UIA 调用，纯 Win32，比枚举快两个数量级
if (-not ([System.Management.Automation.PSTypeName]'DshCuWin32').Type) {
    Add-Type -Namespace '' -Name 'DshCuWin32' -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern System.IntPtr GetForegroundWindow();
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern System.IntPtr SendMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
'@
}

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- 元素缓存
# 每次 snapshot 都重建缓存并把 gen 自增：旧 ref 一律按"过期"处理。
# 这是刻意的：UIA 元素会随界面变化失效，而**失效的元素常常不报错、只是返回空属性**，
# 拿它做判断会得到静默的错答案。宁可让调用方重新 capture 一次。
$script:Elements = @{}
$script:Gen = 0

$InteractiveRoles = @('Text', 'Edit', 'Button', 'CheckBox', 'RadioButton', 'ComboBox',
    'ListItem', 'MenuItem', 'TabItem', 'Hyperlink', 'Slider', 'Spinner',
    'DataItem', 'TreeItem', 'SplitButton', 'Thumb')

function Write-Resp($obj) {
    $json = $obj | ConvertTo-Json -Depth 12 -Compress
    [Console]::Out.WriteLine($json)
    [Console]::Out.Flush()
}

function Fail($id, $code, $message) {
    Write-Resp @{ id = $id; ok = $false; code = $code; message = $message }
}

function Get-TopWindow($pattern) {
    if ([string]::IsNullOrWhiteSpace($pattern)) {
        $h = [DshCuWin32]::GetForegroundWindow()
        if ($h -eq [System.IntPtr]::Zero) { return $null }
        try {
            $el = [System.Windows.Automation.AutomationElement]::FromHandle($h)
            if ($el) {
                return [pscustomobject]@{ el = $el; name = $el.Current.Name; pid = $el.Current.ProcessId }
            }
        } catch { }
        return $null
    }
    # 指定了窗口名：只枚举顶层 Window，进程名用一次性映射（不逐个 Get-Process）
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Window)
    $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
    $cands = @()
    foreach ($w in $wins) {
        try {
            $n = $w.Current.Name
            if ([string]::IsNullOrWhiteSpace($n)) { continue }
            $cands += [pscustomobject]@{ el = $w; name = $n; pid = $w.Current.ProcessId }
        } catch { }
    }
    $hit = @($cands | Where-Object { $_.name -like "*$pattern*" })
    if ($hit.Count -gt 0) { return $hit[0] }
    $map = @{}
    try {
        foreach ($p in (Get-Process -ErrorAction SilentlyContinue |
                        Select-Object Id, ProcessName)) { $map[[int]$p.Id] = $p.ProcessName }
    } catch { }
    foreach ($c in $cands) {
        $pn = $map[[int]$c.pid]
        if ($pn -and $pn -like "*$pattern*") { return $c }
    }
    return $null
}

function Get-ElInfo($el, $ref, $deep) {
    $c = $el.Current
    $r = $c.BoundingRectangle
    $role = [string]$c.ControlType.ProgrammaticName
    if ($role.StartsWith('ControlType.')) { $role = $role.Substring(12) }
    $info = @{
        ref     = $ref
        name    = [string]$c.Name
        role    = $role
        cls     = [string]$c.ClassName
        rect    = @{ x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height }
        cx      = [int]($r.X + $r.Width / 2)
        cy      = [int]($r.Y + $r.Height / 2)
        enabled = [bool]$c.IsEnabled
        focused = [bool]$c.HasKeyboardFocus
        offscreen = [bool]$c.IsOffscreen
        keyboard_focusable = [bool]$c.IsKeyboardFocusable
        password = $false
        accelerator = [string]$c.AcceleratorKey
        automation_id = [string]$c.AutomationId
        value   = $null
        toggle  = $null
        patterns = @()
    }
    try { $info.password = [bool]$c.IsPassword } catch { }
    if (-not $deep) { return $info }
    # 只有"值得编号"的节点才探模式：每个 pattern 一次跨进程调用，很贵
    $pats = New-Object System.Collections.ArrayList
    try {
        if ($el.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$null)) {
            [void]$pats.Add('InvokePattern')
        }
    } catch { }
    try {
        if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$null)) {
            [void]$pats.Add('ValuePattern')
            try {
                $vp = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
                $info.value = [string]$vp.Current.Value
            } catch { }
        }
    } catch { }
    try {
        if ($el.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$null)) {
            [void]$pats.Add('TogglePattern')
            try {
                $tp = $el.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
                $info.toggle = $tp.Current.ToggleState.ToString()
            } catch { }
        }
    } catch { }
    foreach ($pr in @(
            @('SelectionItemPattern', [System.Windows.Automation.SelectionItemPattern]),
            @('ExpandCollapsePattern', [System.Windows.Automation.ExpandCollapsePattern]),
            @('ScrollItemPattern', [System.Windows.Automation.ScrollItemPattern]),
            @('RangeValuePattern', [System.Windows.Automation.RangeValuePattern]),
            @('WindowPattern', [System.Windows.Automation.WindowPattern]))) {
        try {
            if ($el.TryGetCurrentPattern($pr[1]::Pattern, [ref]$null)) { [void]$pats.Add($pr[0]) }
        } catch { }
    }
    $info.patterns = $pats
    return $info
}

# 值不值得给编号：对齐豆包 VisualTree 的思路——只对有语义/可交互的节点编号，
# 纯布局容器（Pane/Custom/Group 且没名字）不进 ref 表，否则 ref 表会被噪声塞满。
function Test-Interesting($info) {
    if ($info.offscreen) { return $false }
    if ($info.rect.w -le 0 -or $info.rect.h -le 0) { return $false }
    if ($InteractiveRoles -contains $info.role) { return $true }
    if (-not [string]::IsNullOrWhiteSpace($info.name)) { return $true }
    return $false
}

function Invoke-Snapshot($req) {
    $script:Gen = $script:Gen + 1
    $script:Elements = @{}
    $win = Get-TopWindow $req.window
    if (-not $win) {
        Fail $req.id 'CU_NO_WINDOW' 'UIA 里找不到标题或进程名匹配的顶层窗口（也可能这个应用没暴露可访问性树）'
        return $null
    }
    $depth = 12
    if ($req.depth) { $depth = [int]$req.depth }
    $limit = 400
    if ($req.limit) { $limit = [int]$req.limit }
    $maxms = 15000
    if ($req.maxms) { $maxms = [int]$req.maxms }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    $refs = New-Object System.Collections.ArrayList
    $byRef = @{}
    $count = 0
    $clipped = $false
    $budget = $false

    # 迭代遍历（显式栈）而不是递归：PowerShell 里嵌套函数对父作用域的赋值是
    # **本地副本**，`$n++` 不会改到外层，递归写法会静默数错。栈式写法没有这个坑。
    $stack = New-Object System.Collections.Stack
    $stack.Push(@($win.el, 0))
    while ($stack.Count -gt 0) {
        if ($sw.ElapsedMilliseconds -gt $maxms) { $budget = $true; break }
        if ($count -ge $limit) { $clipped = $true; break }
        $frame = $stack.Pop()
        $el = $frame[0]
        $d = $frame[1]
        $count = $count + 1
        $ref = "e$count"
        $info = $null
        try { $info = Get-ElInfo $el $ref $false } catch { $count = $count - 1; continue }
        if (Test-Interesting $info) {
            # 第二轮才探模式：只有候选节点付得起这个代价
            try { $info = Get-ElInfo $el $ref $true } catch { }
            $byRef[$ref] = $el
            [void]$refs.Add($info)
        } else {
            $count = $count - 1
        }
        if ($d -ge $depth) { continue }
        $kids = New-Object System.Collections.ArrayList
        try {
            $child = $walker.GetFirstChild($el)
            while ($child) {
                [void]$kids.Add($child)
                $child = $walker.GetNextSibling($child)
            }
        } catch { }
        for ($i = $kids.Count - 1; $i -ge 0; $i--) { $stack.Push(@($kids[$i], $d + 1)) }
    }
    $script:Elements = $byRef
    Write-Resp @{
        id = $req.id; ok = $true; gen = $script:Gen
        window = @{ name = $win.name; pid = $win.pid }
        refs = $refs
        truncated = $clipped
        budget_exceeded = $budget
        examined = $count
        elapsed_ms = [int]$sw.ElapsedMilliseconds
        limit = $limit
    }
}

function Get-El($ref) {
    if (-not $ref) { throw 'ref 为空' }
    $m = [regex]::Match([string]$ref, 'e(\d+)')
    if (-not $m.Success) { throw 'ref 形状不合法（要 e12 这种）' }
    $key = 'e' + $m.Groups[1].Value
    if (-not $script:Elements.ContainsKey($key)) {
        throw "ref $key 不在本次快照里（快照已过期或编号不存在），请重新 state-capture"
    }
    return $script:Elements[$key]
}

function Invoke-Action($req) {
    # 注意：错误分支**必须 Write-Resp**，不能只 `return @{...}`。
    # 只 return 的话响应永远不写出去，Python 侧会一直等到 25s 超时，
    # 再把"元素拒绝了这个动作"误报成"UIA 子进程没响应"——错误码和恢复建议全丢。
    try { $el = Get-El $req.ref } catch {
        Fail $req.id 'CU_ELEMENT' $_.Exception.Message
        return $null
    }
    $a = $req.action
    $v = $req.value
    try {
        switch ($a) {
            'set_focus' { $el.SetFocus() }
            'invoke'    { $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
            'set_value' { $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue([string]$v) }
            'toggle'    {
                $p = $el.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
                # 幂等：目标状态已经达到就不再切（重复点开关会把用户的状态反掉）
                if ($v -ne $null -and $v -ne '') {
                    $want = ([string]$v).ToLower()
                    $cur = $p.Current.ToggleState.ToString().ToLower()
                    if (($want -eq 'on' -and $cur -eq 'on') -or ($want -eq 'off' -and $cur -eq 'off')) {
                        Write-Resp @{ id = $req.id; ok = $true; noop = $true; state = $cur }
                        return $null
                    }
                }
                $p.Toggle()
            }
            'select'    { $el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select() }
            'add_to_selection' { $el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).AddToSelection() }
            'remove_from_selection' { $el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).RemoveFromSelection() }
            'expand'    { $el.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Expand() }
            'collapse'  { $el.GetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern).Collapse() }
            'scroll_into_view' { $el.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView() }
            'set_range_value' {
                $el.GetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern).SetValue([double]$v)
            }
            'window_close' { $el.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern).Close() }
            default {
                Fail $req.id 'CU_ELEMENT' "不支持的元素动作 $a"
                return $null
            }
        }
    } catch {
        $msg = $_.Exception.Message
        # ---- 消息级兜底（BM_CLICK）----
        # 在**非输入桌面**上 UIA 的 Invoke() 会被拒绝（实测 WinForms 抛
        # "Hot key is already registered"）；`SendInput` 更是直接被系统拒绝。
        # 但 `BM_CLICK` 是发给窗口的消息，不依赖输入桌面，也不依赖前台——
        # 这是隔离桌面里唯一稳定的"点按钮"手段，所以这里补一条兜底。
        if ($a -eq 'invoke') {
            try {
                $h = [IntPtr]$el.Current.NativeWindowHandle
                if ($h -ne [IntPtr]::Zero) {
                    [void][DshCuWin32]::SendMessage($h, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)
                    $info2 = $null
                    try { $info2 = Get-ElInfo $el $req.ref $true } catch { }
                    Write-Resp @{ id = $req.id; ok = $true; method = 'bm_click'; element = $info2 }
                    return $null
                }
            } catch { }
        }
        Fail $req.id 'CU_ELEMENT' ("动作 $a 被该元素拒绝：$msg。先 --get pattern 看它支持哪些模式")
        return $null
    }
    $info = $null
    try { $info = Get-ElInfo $el $req.ref $true } catch { }
    Write-Resp @{ id = $req.id; ok = $true; element = $info }
}

function Invoke-Get($req) {
    try { $el = Get-El $req.ref } catch {
        Fail $req.id 'CU_ELEMENT' $_.Exception.Message
        return $null
    }
    try { $info = Get-ElInfo $el $req.ref $true } catch {
        Fail $req.id 'CU_ELEMENT' "元素已失效：$($_.Exception.Message)"
        return $null
    }
    $prop = $req.prop
    if (-not $prop) { Write-Resp @{ id = $req.id; ok = $true; element = $info }; return $null }
    if (-not $info.ContainsKey($prop)) {
        Fail $req.id 'CU_ELEMENT' "没有属性 $prop"
        return $null
    }
    Write-Resp @{ id = $req.id; ok = $true; prop = $prop; value = $info[$prop] }
}

function Invoke-At($req) {
    $pt = New-Object System.Windows.Point([double]$req.x, [double]$req.y)
    $el = [System.Windows.Automation.AutomationElement]::FromPoint($pt)
    if (-not $el) { Fail $req.id 'CU_ELEMENT' '这个点上没有元素'; return $null }
    $script:Gen = $script:Gen + 1
    $script:Elements = @{}
    $info = Get-ElInfo $el 'e1' $true
    $script:Elements['e1'] = $el
    Write-Resp @{ id = $req.id; ok = $true; gen = $script:Gen; element = $info }
}

# ---------------------------------------------------------------- 主循环
while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    $req = $null
    try { $req = $line | ConvertFrom-Json } catch {
        Write-Resp @{ id = 0; ok = $false; code = 'CU_PROTOCOL'; message = '请求不是合法 JSON' }
        continue
    }
    try {
        switch ($req.op) {
            'ping'     { Write-Resp @{ id = $req.id; ok = $true; pid = $PID; gen = $script:Gen } }
            'snapshot' { Invoke-Snapshot $req | Out-Null }
            'at'       { Invoke-At $req | Out-Null }
            'get'      { Invoke-Get $req | Out-Null }
            'act'      { Invoke-Action $req | Out-Null }
            'resync'   {
                $script:Elements = @{}
                $script:Gen = $script:Gen + 1
                Write-Resp @{ id = $req.id; ok = $true; gen = $script:Gen }
            }
            'quit'     { Write-Resp @{ id = $req.id; ok = $true }; exit 0 }
            default    { Fail $req.id 'CU_PROTOCOL' "不认识 op=$($req.op)" }
        }
    } catch {
        Fail $req.id 'CU_ELEMENT' $_.Exception.Message
    }
}
