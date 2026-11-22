#!/bin/zsh
cd -- "${0:A:h}" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v python3 >/dev/null 2>&1; then
  print '未找到 Python 3。请先安装 Python 3.9 或以上版本，再双击启动。'
  print '下载地址：https://www.python.org/downloads/macos/'
  read -r '?按回车键关闭…'
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  print '需要 Python 3.9 或以上版本，请更新后再运行。'
  read -r '?按回车键关闭…'
  exit 1
fi
python3 app.py "$@"
print '\n本机服务已关闭。'
read -r '?按回车键关闭此窗口…'
