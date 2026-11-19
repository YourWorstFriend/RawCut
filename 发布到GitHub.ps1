# 把本地仓库推到你自己的 GitHub 仓库
# 用法：  .\发布到GitHub.ps1 -RepoUrl "https://github.com/你的用户名/ff-editor.git"
#
# 第一次推送会弹出登录窗口（Git Credential Manager），
# 用浏览器授权即可，不需要把密码/令牌写给任何人。

param(
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl,

    [string]$Tag = "v1.0.0"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".git")) {
    Write-Host "这里还不是 git 仓库，先初始化…"
    git init -b main | Out-Null
}

# 配置远端
git remote remove origin 2>$null
git remote add origin $RepoUrl

Write-Host "推送 main…"
git push -u origin main

Write-Host "推送标签 $Tag…"
git tag $Tag 2>$null
git push origin $Tag

Write-Host ""
Write-Host "代码已推送。发布版（exe 压缩包）需要在大文件上传："
Write-Host "  1) 打开 $RepoUrl/releases/new"
Write-Host "  2) Tag 选 $Tag，标题写「FF剪辑 $Tag」"
Write-Host "  3) 附件拖入 dist\FF剪辑-绿色版.zip"
Write-Host "  4) 点 Publish release"
