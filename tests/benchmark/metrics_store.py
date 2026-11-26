#!/usr/bin/env python3
"""归档和回看 Prometheus 指标数据目录。

用法：
  python3 tests/benchmark/metrics_store.py archive --source <数据目录> --run-dir <运行证据目录>
  python3 tests/benchmark/metrics_store.py view --run-dir <运行证据目录> --view-dir <回看副本目录>

archive 把 Prometheus 本地 TSDB 目录复制到 ``RUN_DIR/metrics.tmp``，校验后原子重命名为
``RUN_DIR/metrics``。失败时保留 ``metrics.tmp``，不覆盖已有 ``metrics`` 或
``metrics.tmp``。复制、校验或重命名失败且证据文件已存在时，会在
``.metrics-evidence.lock`` 锁下合并顶层 ``archive_error``、``status`` 和 ``missing``；
证据文件不存在时不会创建它。view 把 ``RUN_DIR/metrics`` 的内容复制到 ``VIEW_DIR``，
复制前后校验归档目录不变，且不修改归档内容或证据文件。

源目录、归档 ``metrics/`` 和回看所用的 ``RUN_DIR/metrics/`` 本身是符号链接时一律拒绝。

退出码：
  0  成功；
  2  参数、前置条件或文件 IO 错误；
  3  复制后校验失败（文件数或树哈希不一致，或归档目录在校验期间变化）。

树哈希按 POSIX 相对路径排序，对每个常规文件依次加入路径和文件内容的 SHA-256；目录
本身不计入哈希。符号链接和特殊文件一律拒绝，避免归档静默缺失或越界读取。
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VERIFY = 3

HASH_NAME = "sha256"
CHUNK_SIZE = 1024 * 1024

EVIDENCE_NAME = "metrics-evidence.json"
EVIDENCE_LOCK_NAME = ".metrics-evidence.lock"

ARCHIVE_ERROR_COPY = "归档复制失败"
ARCHIVE_ERROR_VERIFY = "归档校验失败"
ARCHIVE_ERROR_RENAME = "归档重命名失败"


class UnsupportedEntryError(Exception):
    """源目录包含符号链接或非普通文件。"""


def _error(message):
    print("metrics_store: %s" % message, file=sys.stderr)


def _walk(root):
    def onerror(error):
        raise error

    return os.walk(root, topdown=True, onerror=onerror, followlinks=False)


def _iter_regular_files(root):
    """遍历常规文件，遇到符号链接或特殊文件立即报错。"""
    for dirpath, dirnames, filenames in _walk(root):
        dirnames.sort()
        filenames.sort()
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                raise UnsupportedEntryError("符号链接目录: %s" % full)
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                raise UnsupportedEntryError("符号链接文件: %s" % full)
            if not os.path.isfile(full):
                raise UnsupportedEntryError("特殊文件: %s" % full)
            yield os.path.relpath(full, root), full


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.digest()


def tree_stat(root):
    """返回 ``(树哈希十六进制字符串, 常规文件数)``。

    文件按 POSIX 相对路径排序；哈希输入为 ``F\0路径\0文件内容SHA256``。路径和固定
    长度的摘要使不同文件的边界无歧义。
    """
    entries = []
    for relative, full in _iter_regular_files(root):
        entries.append((relative.replace(os.sep, "/"), _file_sha256(full)))
    entries.sort(key=lambda item: item[0])

    digest = hashlib.sha256()
    for relative, file_digest in entries:
        digest.update(b"F\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest)
    return digest.hexdigest(), len(entries)


def tree_hash(root):
    """返回目录树哈希十六进制字符串。"""
    return tree_stat(root)[0]


def count_files(root):
    """返回目录树中的常规文件数。"""
    return sum(1 for _ in _iter_regular_files(root))


def copy_tree(source, destination):
    """复制 source 的内容到 destination；destination 必须不存在。

    使用 ``shutil.copy2`` 保留文件权限和时间戳，目录使用 ``copystat`` 保留元数据。
    """
    if os.path.lexists(destination):
        raise FileExistsError("目标已存在: %s" % destination)
    os.makedirs(destination)
    copy_tree_contents(source, destination)
    shutil.copystat(source, destination)


def copy_tree_contents(source, destination):
    """把 source 的内容复制进已存在的 destination，不创建额外的 metrics/ 层。"""
    if not os.path.isdir(destination):
        raise NotADirectoryError("目标不是目录: %s" % destination)

    for name in sorted(os.listdir(source)):
        source_path = os.path.join(source, name)
        destination_path = os.path.join(destination, name)
        if os.path.islink(source_path):
            raise UnsupportedEntryError("符号链接: %s" % source_path)
        if os.path.isdir(source_path):
            shutil.copytree(
                source_path,
                destination_path,
                symlinks=False,
                copy_function=shutil.copy2,
            )
        elif os.path.isfile(source_path):
            shutil.copy2(source_path, destination_path)
        else:
            raise UnsupportedEntryError("特殊文件: %s" % source_path)


def _format_hash(value):
    return "%s:%s" % (HASH_NAME, value)


def _paths_overlap(first, second):
    """两个路径相同，或其中一个位于另一个之内时返回 True。"""
    first_real = os.path.realpath(first)
    second_real = os.path.realpath(second)
    try:
        common = os.path.commonpath([first_real, second_real])
    except ValueError:
        return False
    return common == first_real or common == second_real


def _short_text(value, limit=160):
    text = " ".join(str(value).split())
    if not text:
        return "未知错误"
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _write_json_atomic(path, document):
    """写同目录临时文件并 fsync，再用 os.replace 原子替换。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    descriptor, temporary = tempfile.mkstemp(
        prefix=".metrics-evidence-", suffix=".tmp", dir=directory
    )
    try:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            mode = None
        if mode is not None:
            try:
                os.fchmod(descriptor, mode)
            except OSError:
                pass
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
        except OSError:
            # 同一目录内的 rename 仍是原子替换；保留 os.replace 作为首选路径。
            os.rename(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _merge_archive_error(evidence_path, reason, detail):
    """在证据对象上合并失败字段；调用方必须已持有证据锁。"""
    try:
        with open(evidence_path, "r", encoding="utf-8") as handle:
            evidence = json.load(handle)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as error:
        _error("无法更新指标证据（读取失败）: %s" % error)
        return

    if not isinstance(evidence, dict):
        _error("无法更新指标证据（顶层不是 JSON 对象）: %s" % evidence_path)
        return

    evidence["archive_error"] = reason
    evidence["status"] = "incomplete"
    missing = evidence.get("missing")
    if isinstance(missing, list):
        missing = list(missing)
    elif missing is None:
        missing = []
    else:
        missing = [str(missing)]
    missing.append(
        "指标归档失败（%s）：%s；请人工处理保留的 metrics.tmp。"
        % (reason, _short_text(detail))
    )
    evidence["missing"] = missing

    try:
        _write_json_atomic(evidence_path, evidence)
    except (OSError, TypeError, ValueError) as error:
        _error("无法更新指标证据（写入失败）: %s" % error)


def _record_archive_error(run_dir, reason, detail):
    """在 0600、不可跟随符号链接的锁下合并 metrics-evidence.json。

    证据文件不存在时不创建它；任何记录失败都只打印警告，不改变归档命令的退出码。
    """
    evidence_path = os.path.join(run_dir, EVIDENCE_NAME)
    if not os.path.lexists(evidence_path):
        return

    lock_path = os.path.join(run_dir, EVIDENCE_LOCK_NAME)
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        _error("无法记录 archive_error（打开锁失败）: %s" % error)
        return

    try:
        try:
            info = os.fstat(lock_descriptor)
            if not stat.S_ISREG(info.st_mode):
                _error("无法记录 archive_error（锁不是常规文件）: %s" % lock_path)
                return
            os.fchmod(lock_descriptor, 0o600)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        except OSError as error:
            _error("无法记录 archive_error（获取锁失败）: %s" % error)
            return

        try:
            _merge_archive_error(evidence_path, reason, detail)
        except Exception as error:
            _error("无法更新指标证据: %s" % error)
        finally:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(lock_descriptor)


def archive(source, run_dir):
    """把 Prometheus 数据目录归档到 ``run_dir/metrics``。"""
    source = os.path.abspath(source)
    run_dir = os.path.abspath(run_dir)

    if os.path.islink(source):
        _error("源目录是符号链接，拒绝归档: %s" % source)
        return EXIT_USAGE
    if not os.path.isdir(source):
        _error("源目录不存在或不是目录: %s" % source)
        return EXIT_USAGE
    if not os.path.isdir(os.path.join(source, "wal")):
        _error("源目录缺少 wal/ 子目录，不是可归档的 Prometheus 数据目录: %s" % source)
        return EXIT_USAGE
    if not os.path.isdir(run_dir):
        _error("运行证据目录不存在或不是目录: %s" % run_dir)
        return EXIT_USAGE

    target = os.path.join(run_dir, "metrics")
    temporary = os.path.join(run_dir, "metrics.tmp")
    for path, label in ((target, "目标 metrics"), (temporary, "临时目录 metrics.tmp")):
        if os.path.islink(path):
            _error("%s是符号链接，拒绝覆盖: %s" % (label, path))
            return EXIT_USAGE
    if os.path.lexists(target):
        _error("目标已存在，拒绝覆盖: %s" % target)
        return EXIT_USAGE
    if os.path.lexists(temporary):
        _error("临时目录已存在，拒绝覆盖: %s" % temporary)
        return EXIT_USAGE
    for path in (target, temporary):
        if _paths_overlap(source, path):
            _error("归档目标与源目录重叠，拒绝复制: %s" % path)
            return EXIT_USAGE

    try:
        source_hash, source_count = tree_stat(source)
    except UnsupportedEntryError as error:
        _error("源目录包含符号链接或特殊文件，拒绝归档: %s" % error)
        return EXIT_USAGE
    except OSError as error:
        _error("读取源目录失败: %s" % error)
        return EXIT_USAGE

    try:
        copy_tree(source, temporary)
    except UnsupportedEntryError as error:
        _error("复制时遇到符号链接或特殊文件，保留临时目录: %s" % error)
        _record_archive_error(run_dir, ARCHIVE_ERROR_COPY, error)
        return EXIT_USAGE
    except (OSError, shutil.Error) as error:
        _error("复制到 %s 失败，保留临时目录供人工处理: %s" % (temporary, error))
        _record_archive_error(run_dir, ARCHIVE_ERROR_COPY, error)
        return EXIT_USAGE

    try:
        has_wal = os.path.isdir(os.path.join(temporary, "wal"))
        current_hash, current_count = tree_stat(source)
        target_hash, target_count = tree_stat(temporary)
    except UnsupportedEntryError as error:
        _error("复制后校验失败，保留 %s: %s" % (temporary, error))
        _record_archive_error(run_dir, ARCHIVE_ERROR_VERIFY, error)
        return EXIT_VERIFY
    except OSError as error:
        _error("复制后校验失败，保留 %s: %s" % (temporary, error))
        _record_archive_error(run_dir, ARCHIVE_ERROR_VERIFY, error)
        return EXIT_VERIFY

    if not has_wal:
        _error("复制后校验失败: %s 缺少 wal/，保留临时目录" % temporary)
        _record_archive_error(run_dir, ARCHIVE_ERROR_VERIFY, "副本缺少 wal/ 子目录")
        return EXIT_VERIFY
    if current_hash != source_hash or current_count != source_count:
        _error("复制期间源目录发生变化，校验失败，保留 %s" % temporary)
        _record_archive_error(run_dir, ARCHIVE_ERROR_VERIFY, "源目录在复制期间发生变化")
        return EXIT_VERIFY
    if target_count != source_count or target_hash != source_hash:
        _error(
            "复制后校验失败: 文件数或树哈希不一致（源 %d 个文件，副本 %d 个文件），保留 %s"
            % (source_count, target_count, temporary)
        )
        _record_archive_error(
            run_dir,
            ARCHIVE_ERROR_VERIFY,
            "文件数或树哈希不一致（源 %d 个文件，副本 %d 个文件）"
            % (source_count, target_count),
        )
        return EXIT_VERIFY

    try:
        os.replace(temporary, target)
    except OSError as error:
        _error("重命名为 %s 失败，保留 %s: %s" % (target, temporary, error))
        _record_archive_error(run_dir, ARCHIVE_ERROR_RENAME, error)
        return EXIT_USAGE

    print("归档完成: %s" % target)
    print("源目录树哈希: %s" % _format_hash(source_hash))
    print("目标目录树哈希: %s" % _format_hash(target_hash))
    print("归档文件数: %d" % target_count)
    print(
        "复制策略: shutil.copy2 保留文件权限与时间戳，目录使用 copystat；"
        "符号链接拒绝；校验通过后 os.replace 原子重命名。"
    )
    return EXIT_OK


def view(run_dir, view_dir):
    """把 ``run_dir/metrics`` 的内容复制到 ``view_dir``，归档目录保持只读。"""
    run_dir = os.path.abspath(run_dir)
    view_dir = os.path.abspath(view_dir)
    metrics = os.path.join(run_dir, "metrics")

    if os.path.islink(metrics):
        _error("归档指标目录是符号链接，拒绝读取: %s" % metrics)
        return EXIT_USAGE
    if not os.path.isdir(metrics):
        _error("归档指标目录不存在或不是目录: %s" % metrics)
        return EXIT_USAGE
    if not os.path.isdir(os.path.join(metrics, "wal")):
        _error("归档指标目录缺少 wal/ 子目录: %s" % metrics)
        return EXIT_USAGE

    metrics_real = os.path.realpath(metrics)
    view_real = os.path.realpath(view_dir)
    try:
        inside_archive = os.path.commonpath([metrics_real, view_real]) == metrics_real
    except ValueError:
        inside_archive = False
    if inside_archive:
        _error("回看目录不能位于归档目录内，拒绝写入: %s" % view_dir)
        return EXIT_USAGE

    view_exists = os.path.lexists(view_dir)
    if view_exists:
        if os.path.islink(view_dir) or not os.path.isdir(view_dir):
            _error("回看目录已存在但不是目录: %s" % view_dir)
            return EXIT_USAGE
        if os.listdir(view_dir):
            _error("回看目录非空，拒绝覆盖: %s" % view_dir)
            return EXIT_USAGE

    try:
        before_hash, before_count = tree_stat(metrics)
    except UnsupportedEntryError as error:
        _error("归档目录包含符号链接或特殊文件，拒绝复制: %s" % error)
        return EXIT_USAGE
    except OSError as error:
        _error("读取归档指标目录失败: %s" % error)
        return EXIT_USAGE

    try:
        if view_exists:
            copy_tree_contents(metrics, view_dir)
        else:
            copy_tree(metrics, view_dir)
    except UnsupportedEntryError as error:
        _error("复制到回看目录失败，归档目录未修改: %s" % error)
        return EXIT_USAGE
    except (OSError, shutil.Error) as error:
        _error("复制到回看目录失败，归档目录未修改: %s" % error)
        return EXIT_USAGE

    try:
        after_hash, after_count = tree_stat(metrics)
        copy_hash, copy_count = tree_stat(view_dir)
    except UnsupportedEntryError as error:
        _error("回看副本校验失败: %s" % error)
        return EXIT_VERIFY
    except OSError as error:
        _error("回看副本校验失败: %s" % error)
        return EXIT_VERIFY

    if before_hash != after_hash or before_count != after_count:
        _error("归档目录在复制期间发生变化，校验失败: %s" % metrics)
        return EXIT_VERIFY
    if copy_count != before_count or copy_hash != before_hash:
        _error(
            "回看副本校验失败: 文件数或树哈希不一致（归档 %d 个文件，副本 %d 个文件）"
            % (before_count, copy_count)
        )
        return EXIT_VERIFY

    print("回看副本已准备: %s" % view_dir)
    print("归档目录树哈希: %s" % _format_hash(before_hash))
    print("副本目录树哈希: %s" % _format_hash(copy_hash))
    print("归档文件数: %d" % before_count)
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="metrics_store.py",
        description="归档或复制 Prometheus 指标数据目录，并在复制前后校验文件数与树哈希。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    archive_parser = subparsers.add_parser(
        "archive",
        help="归档 Prometheus 数据目录到运行证据目录",
        description=(
            "把 Prometheus 本地 TSDB 目录复制到 RUN_DIR/metrics.tmp，校验 wal/、"
            "文件数和树哈希后原子重命名为 RUN_DIR/metrics。"
        ),
    )
    archive_parser.add_argument(
        "--source",
        required=True,
        help="Prometheus 本地数据目录，必须存在且包含 wal/ 子目录",
    )
    archive_parser.add_argument(
        "--run-dir",
        required=True,
        help="基准运行证据目录；归档写入其 metrics/ 子目录",
    )

    view_parser = subparsers.add_parser(
        "view",
        help="复制归档指标到回看目录，归档目录保持不变",
        description=(
            "把 RUN_DIR/metrics 的内容复制到 VIEW_DIR（不创建额外的 metrics/ 层），"
            "复制后校验文件数和树哈希，并确认归档目录复制前后不变。"
        ),
    )
    view_parser.add_argument(
        "--run-dir",
        required=True,
        help="包含已归档 metrics/ 子目录的运行证据目录",
    )
    view_parser.add_argument(
        "--view-dir",
        required=True,
        help="回看副本目录；必须不存在或为空目录",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "archive":
        return archive(args.source, args.run_dir)
    if args.command == "view":
        return view(args.run_dir, args.view_dir)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
