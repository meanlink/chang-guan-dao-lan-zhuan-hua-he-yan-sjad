"""离线核验场馆凭据核验包。

用法（在 backend 目录下，不需要数据库、Redis 或正在运行的后端）：

    python -m scripts.ledger_verify 核验包.json [--anchor 锚点.json] [--key-id 指纹] [--json]

核验结果：
- 完整      链首尾衔接、逐条摘要正确，且与独立签名的可信锚点一致
- 受损      改写、缺项、调序、尾部截断、整包重打包等
- 缺少可信依据  链本身自洽，但没有提供锚点（无法排除整包重建/截尾）
"""
import argparse
import json
import sys

from src.services import ledger_package


def load(path):
    with open(path, encoding='utf-8') as stream:
        return json.load(stream)


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线核验场馆凭据核验包')
    parser.add_argument('package', help='核验包 JSON 路径')
    parser.add_argument('--anchor', help='负责人另存的可信锚点 JSON 路径')
    parser.add_argument('--key-id', help='预先登记的可信公钥指纹，防止锚点被替换')
    parser.add_argument('--json', action='store_true', help='输出机器可读 JSON')
    args = parser.parse_args(argv)

    try:
        package = load(args.package)
        anchor = load(args.anchor) if args.anchor else None
    except (OSError, ValueError) as error:
        print(f'无法读取文件：{error}', file=sys.stderr)
        return 2

    report = ledger_package.verify_package_document(package, anchor, args.key_id)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f'状态：{report["label"]}（{report["status"]}）')
        print(report['summary'])
        for problem in report['problems']:
            seq = f' 第 {problem["seq"]} 条' if problem.get('seq') else ''
            print(f'- [{problem["code"]}]{seq} {problem["message"]}')
        anchor_state = report['anchor']
        if anchor_state.get('checked'):
            print(f'锚点签名：{"有效" if anchor_state.get("valid") else "无效"}（公钥指纹 {anchor_state.get("key_id")}）')
        else:
            print('锚点：未提供')
    return 0 if report['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
