# Aria2 Python 下载器

面向大模型文件的多分段、可续传 HTTP 下载器。默认把文件保存到 `E:\ModeLs`，并可在下载完成后执行 SHA-256 完整性校验。

> 本项目与官方 [aria2](https://github.com/aria2/aria2) 不是同一个项目。

## 可靠性保障

- 严格验证 `206 Partial Content` 与 `Content-Range`，服务器忽略分段请求时自动安全降级为单流下载。
- 每个线程只能写入自己的字节区间，提前断流会重试，不会被误判为成功。
- `.aria2.json` 控制文件记录精确进度；URL、大小、ETag 或分段布局不匹配时安全重启。
- 只有每个分段都达到精确长度才会报告完成并删除控制文件。
- `--sha256` 可核对 Hugging Face 提供的 LFS SHA-256；不匹配时返回失败并重置断点进度。
- 使用系统 CA 验证 HTTPS 证书。

## 环境与安装

需要 Python 3.9 或更高版本，无第三方运行时依赖。

```powershell
git clone https://github.com/KolentoMa/Aria2.git
cd Aria2
python -m pip install -e .
```

也可以不安装，直接在项目目录运行 `python -m aria2`。

## 使用

```powershell
# 文件名从 URL 提取，默认保存到 E:\ModeLs
python -m aria2 "https://example.com/model.gguf"

# 8 个分段、10 次重试，并验证 SHA-256
python -m aria2 -s 8 -r 10 --sha256 <64位哈希> "https://example.com/model.gguf"

# 指定完整输出路径
python -m aria2 -o "E:\ModeLs\model.gguf" "https://example.com/model.gguf"

# 忽略断点并从头下载
python -m aria2 --no-resume "https://example.com/model.gguf"
```

只给 `-o` 传文件名时，它仍会放入 `E:\ModeLs`。传入含目录的完整路径时使用该路径。可用环境变量覆盖默认目录：

```powershell
$env:ARIA2_MODEL_DIR = "F:\AI\Models"
```

Windows 用户也可以双击：

- `download.bat`：交互式批量下载任意直链。
- `download_model.bat`：下载并校验预设的 Qwen3.6 27B GGUF 模型。

## 已验证模型

`download_model.bat` 当前使用：

- 仓库：`DhruvalLabs/Qwen3.6-27B-GGUF`
- 文件：`Qwen3.6-27B-Q3_K_M.gguf`
- 大小：13,500,735,744 字节（约 12.57 GiB）
- SHA-256：`06e2b050c41a741338824e4b9b0b94a49795832fba1d0daeca492033a42d7bf8`

该文件已用 llama.cpp b9982 成功加载并完成最小文本生成测试。运行示例：

```powershell
llama-cli -m "E:\ModeLs\Qwen3.6-27B-Q3_K_M.gguf" -ngl 99 -c 4096
```

## 命令行参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `url` | 必填 | 文件直链 |
| `-o, --output` | `E:\ModeLs\<URL文件名>` | 输出路径 |
| `-s, --segments` | 4 | 并行分段数，范围 1–32 |
| `-r, --retries` | 5 | 首次请求失败后的重试次数 |
| `--no-resume` | 关闭 | 丢弃已有断点并重新下载 |
| `--max-redirects` | 10 | 最大 HTTP 重定向次数 |
| `--sha256 HEX` | 无 | 预期 SHA-256 完整哈希 |

## Python API

```python
from aria2 import download

ok = download(
    "https://example.com/model.gguf",
    segments=8,
    retries=10,
    sha256="...64 hexadecimal characters...",
)
```

未传 `output` 时同样使用默认模型目录。

## 测试

```powershell
python -m unittest discover -v
```

测试覆盖分段下载、服务器忽略 Range、提前断流重试、错误 Content-Range、SHA-256 成功/失败、断点元数据不匹配及小文件分段。

## 许可证

MIT
