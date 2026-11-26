"""校验一次基准运行的指标证据（骨架）。

最终退出码约定：
- 0：成功，证据完整；
- 3：证据不完整；
- 2：参数或 IO 错误；
- 4：Prometheus 不可达。

当前仅为 argparse 骨架：参数解析成功后打印 not implemented 并返回 2。
"""

import argparse
import sys


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="检查基准运行目录中的 Prometheus 指标证据。"
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="基准运行目录，包含测量阶段和待核验的证据文件",
    )
    parser.add_argument(
        "--prometheus",
        default="http://127.0.0.1:9090",
        help="Prometheus 基础地址（默认 http://127.0.0.1:9090）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10,
        help="单次 Prometheus 请求超时秒数（默认 10）",
    )
    return parser.parse_args(argv)


def main(argv=None):
    parse_args(argv)
    print("not implemented")
    return 2


if __name__ == "__main__":
    sys.exit(main())
