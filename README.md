# aria2 — 轻量级断点下载管理器

一个基于 Python 的零依赖下载管理器，支持多线程分段并行下载与断点续传。灵感来源于 [aria2](https://github.com/aria2/aria2)。

## 特性

- **断点续传** — 下载中断后，重新运行相同命令即可从断点处继续，无需重新下载
- **多线程分段下载** — 将文件切分成多个分段，并行下载以提升速度
- **零依赖** — 纯 Python 3 标准库实现，无需 `pip install` 任何第三方包
- **实时进度显示** — 每个分段独立进度条，显示下载速度、剩余时间、整体进度
- **控制文件** — JSON 格式的 `.aria2.json` 文件记录每个分段的下载进度，确保可靠续传
- **预分配输出文件** — 使用稀疏文件技术，不浪费磁盘空间
- **智能重试** — 针对连接中断等临时错误，采用指数退避 + 随机抖动策略自动重试
- **跨平台** — 支持 Windows 和 Linux，ANSI 终端显示，UTF-8 安全

## 安装

```bash
git clone https://github.com/your/aria2.git
cd aria2
pip install -e .
```

环境要求：**Python 3.9+** 即可。

## 使用方法

```bash
# 基础下载
aria2 https://example.com/large-file.zip

# 指定输出文件名
aria2 -o myfile.zip https://example.com/large-file.zip

# 使用 8 个线程并行下载，速度更快
aria2 -s 8 https://example.com/large-file.zip

# 网络不稳定时，增加重试次数
aria2 -r 10 https://example.com/large-file.zip

# 强制重新下载（忽略已有的部分文件）
aria2 --no-resume https://example.com/large-file.zip
```

## 断点续传原理

当你启动下载时，aria2 会在输出文件旁边创建一个 `.aria2.json` 控制文件。这个文件记录了：

- 文件的总大小和来源 URL
- 分段的个数和每个分段的字节范围
- 每个分段已下载完成的字节数

如果下载被中断（Ctrl+C、网络断开、电脑断电），只需重新运行**相同命令**，aria2 将会：

1. 检测到已有的控制文件
2. 验证服务器上的文件是否与上次相同（通过 Content-Length 和 ETag）
3. 从上次中断的精确字节位置继续下载

下载成功后，控制文件会被自动删除。

## 命令行参数

| 参数 | 默认值 | 说明 |
|--------|---------|-------------|
| `url` | _(必填)_ | 要下载的文件 URL |
| `-o, --output FILE` | 从 URL 提取 | 输出文件名 |
| `-s, --segments N` | 4 | 并行下载线程数（最大 32） |
| `-r, --retries N` | 5 | 每个分段的最大重试次数 |
| `--no-resume` | 关闭 | 忽略已有控制文件，强制重新下载 |
| `--max-redirects N` | 10 | 最大 HTTP 重定向次数 |

## 作为 Python 库使用

你也可以在代码中调用 aria2：

```python
from aria2 import download

# 简单调用
download("https://example.com/file.zip", output="myfile.zip")

# 更多控制
from aria2 import DownloadManager

manager = DownloadManager(
    url="https://example.com/file.zip",
    output="myfile.zip",
    segments=8,        # 8 个线程并行
    max_retries=10,    # 最多重试 10 次
)
success = manager.run()
```

## 许可证

MIT
