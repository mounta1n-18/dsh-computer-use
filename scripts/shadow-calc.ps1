# 隔离桌面演示用的计算器：经典 WinForms 窗口（UWP 的 calc 在非输入桌面上起不来）。
# 设计成"UI 显示结果"而不是"内部算完直接报"，这样才能证明是**真的点在按钮上**。
try {
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = 'DSH Shadow Calculator'
$form.Width = 320
$form.Height = 420
$form.StartPosition = 'CenterScreen'
$form.BackColor = [System.Drawing.Color]::FromArgb(28, 30, 36)
$form.ForeColor = [System.Drawing.Color]::White

$display = New-Object System.Windows.Forms.Label
$display.Text = '0'
$display.Font = New-Object System.Drawing.Font('Consolas', 28, [System.Drawing.FontStyle]::Bold)
$display.TextAlign = 'MiddleRight'
$display.Width = 280
$display.Height = 70
$display.Left = 10
$display.Top = 10
$display.BackColor = [System.Drawing.Color]::FromArgb(45, 48, 58)
$form.Controls.Add($display)

$script:acc = $null
$script:op = $null
$script:cur = '0'
$script:fresh = $true

function Update-Display {
    $global:display.Text = $script:cur
}

function Press-Digit([string]$d) {
    if ($script:fresh) { $script:cur = $d; $script:fresh = $false }
    else { $script:cur = $script:cur + $d }
    Update-Display
}

function Press-Op([string]$o) {
    $script:acc = [double]$script:cur
    $script:op = $o
    $script:fresh = $true
}

function Press-Equals {
    if ($null -eq $script:op) { return }
    $b = [double]$script:cur
    $r = switch ($script:op) {
        '+' { $script:acc + $b }
        '-' { $script:acc - $b }
        '*' { $script:acc * $b }
        '/' { if ($b -eq 0) { 'ERR' } else { $script:acc / $b } }
    }
    # 整数值不带 .0，看起来像计算器
    if ($r -is [double] -and [math]::Abs($r - [math]::Round($r)) -lt 1e-9) { $r = [int64][math]::Round($r) }
    $script:cur = [string]$r
    $script:fresh = $true
    $script:op = $null
    Update-Display
}

$keys = @(
    @('7', 10, 100), @('8', 90, 100), @('9', 170, 100), @('/', 10, 180),
    @('4', 90, 180), @('5', 170, 180), @('*', 90, 260),
    @('1', 10, 260), @('2', 170, 260), @('-', 10, 340),
    @('0', 90, 340), @('=', 170, 340), @('+', 90, 340)
)
# 用固定网格，避免上面的手写坐标打架
$layout = @(
    @('7', 10, 100), @('8', 90, 100), @('9', 170, 100), @('/', 250, 100),
    @('4', 10, 180), @('5', 90, 180), @('6', 170, 180), @('*', 250, 180),
    @('1', 10, 260), @('2', 90, 260), @('3', 170, 260), @('-', 250, 260),
    @('0', 10, 340), @('=', 90, 340), @('C', 170, 340), @('+', 250, 340)
)
foreach ($k in $layout) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $k[0]
    $b.Left = $k[1]
    $b.Top = $k[2]
    $b.Width = 70
    $b.Height = 70
    $b.FlatStyle = 'Flat'
    $b.BackColor = [System.Drawing.Color]::FromArgb(58, 62, 74)
    $b.ForeColor = [System.Drawing.Color]::White
    $b.Font = New-Object System.Drawing.Font('Consolas', 16, [System.Drawing.FontStyle]::Bold)
    switch ($k[0]) {
        { $_ -in '0','1','2','3','4','5','6','7','8','9' } { $b.Add_Click({ Press-Digit $this.Text }.GetNewClosure()) }
        { $_ -in '+','-','*','/' } { $b.Add_Click({ Press-Op $this.Text }.GetNewClosure()) }
        '=' { $b.Add_Click({ Press-Equals }) }
        'C' { $b.Add_Click({ $script:cur = '0'; $script:acc = $null; $script:op = $null; $script:fresh = $true; Update-Display }) }
    }
    $form.Controls.Add($b)
}

# 让外部能确认"窗口里真的显示了什么"：显示内容同步到一个文件
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 250
$timer.Add_Tick({
    $path = Join-Path $env:TEMP 'dsh-shadow-calc-display.txt'
    try { [System.IO.File]::WriteAllText($path, $display.Text) } catch { }
})
$timer.Start()

$form.Add_Shown({ [System.IO.File]::WriteAllText((Join-Path $env:TEMP 'dsh-shadow-calc-shown.txt'), 'shown ' + (Get-Date -Format o)) })
[void]$form.ShowDialog()
} catch {
  [System.IO.File]::WriteAllText((Join-Path $env:TEMP 'dsh-shadow-calc-err.txt'), ($_ | Out-String))
}
