"""导出基准运行时间窗内的指标查询结果（骨架）。

最终退出码约定：
- 0：成功，导出完整；
- 3：导出不完整；
- 2：参数或 IO 错误；
- 4：Prometheus 不可达。

当前仅为 argparse 骨架：参数解析成功后打印 not implemented 并返回 2。
"""

import argparse
import sys


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="从 Prometheus 导出基准运行时间窗内的指标查询结果。"
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="基准运行目录，提供时间窗和导出位置上下文",
    )
    parser.add_argument(
        "--prometheus",
        default="http://127.0.0.1:9090",
        help="Prometheus 基础地址（默认 http://127.0.0.1:9090）",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="导出结果目录",
    )
    return parser.parse_args(argv)


def main(argv=None):
    parse_args(argv)
    print("not implemented")
    return 2


if __name__ == "__main__":
    sys.exit(main())
