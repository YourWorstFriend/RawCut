# RawCut —— 项目约定

剪映风格界面 + FFmpeg 剪辑内核的 Windows 桌面剪辑器。Python + tkinter，PyInstaller 打包。

## 结构

- `ffeditor_core.py`：工程模型、滤镜链、逐片段渲染、拼接、代理缓存、无损剪切
- `ffeditor_gui.py`：界面、时间线、播放器、拖放、快捷键
- `dist/RawCut/`：构建产物（gitignored，只作为 Release 附件）
- `_t/`：临时脚本与测试素材（gitignored）

## 常用命令

```powershell
# 本地运行
python ffeditor_gui.py

# 构建（onedir，绿色版）
python -m PyInstaller --noconfirm --clean --onedir --windowed --name RawCut `
  --icon ffeditor.ico --hidden-import PIL._tkinter_finder ffeditor_gui.py

# 自检（不弹界面，验证解码/播放）
dist\RawCut\RawCut.exe --selftest sample.mp4

# 打包
Compress-Archive -Path dist\RawCut -DestinationPath dist\RawCut-1.0.0-win64.zip -Force
```

## 约束

- 界面文案用 MiSans（装了就用，否则退微软雅黑）；时间码用 Consolas 等宽。
- 不改用户素材文件；所有渲染走临时目录。
- 改完必须重新构建 + 自检通过，再更新 README 与 `app.png`。
