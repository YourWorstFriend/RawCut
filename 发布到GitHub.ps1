# 把本地仓库推到你自己的 GitHub 仓库
# 用法：  .\发布到GitHub.ps1 -RepoUrl "https://github.com/YourWorstFriend/RawCut.git"
#
# 第一次推送会弹出浏览器授权（Git Credential Manager），
# 不需要把密码或令牌写给任何人。

param(
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl,

    [string]$Tag = "v1.0.0",

    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".git")) {
    Write-Host "初始化 git 仓库…"
    git init -b $Branch | Out-Null
    git add .gitignore LICENSE README.md ffeditor_core.py ffeditor_gui.py ffeditor.ico app.png screenshot.png ruler.png sample.mp4 cut_gui.py lossless_cut.py 发布到GitHub.ps1
    git -c user.name=FFEditor -c user.email=ffeditor@local commit -m "初始提交：FF剪辑 v1.0"
}

$remotes = git remote
if ($remotes -contains "origin") {
    git remote set-url origin $RepoUrl
} else {
    git remote add origin $RepoUrl
}

if (-not (git tag --list $Tag)) {
    git tag $Tag
}

Write-Host "推送到 $RepoUrl …"
git push -u origin $Branch
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "push 失败。如果建仓库时勾了 README / .gitignore（远端不是空的），先合并再推：" -ForegroundColor Yellow
    Write-Host "  git pull --rebase origin $Branch" -ForegroundColor Yellow
    Write-Host "  git push -u origin $Branch" -ForegroundColor Yellow
    exit 1
}

git push origin $Tag

Write-Host ""
Write-Host "代码已推送。发布版（exe 压缩包）走 Releases：" -ForegroundColor Green
Write-Host "  1) 打开 $RepoUrl/releases/new"
Write-Host "  2) Tag 选 $Tag，标题写「FF剪辑 $Tag」"
Write-Host "  3) 附件拖入 dist\FF剪辑-绿色版.zip（27.9 MB）"
Write-Host "  4) 点 Publish release"
