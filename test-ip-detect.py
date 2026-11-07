#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 install-py.sh 的公网 IP 过滤逻辑
=====================================
把脚本里的两条正则原样抽出来跑，确保测的就是线上那份逻辑
（不是这里另写一份，避免两边漂移）。

Run with:  python test-ip-detect.py
"""

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "server-mirror" / "install-py.sh"

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (("  -> " + str(extra)) if extra else ""), flush=True)


def main():
    if not SCRIPT.exists():
        print("找不到 install-py.sh")
        return 1

    text = SCRIPT.read_text(encoding="utf-8")

    # 从脚本原样抽取两条正则，确保测的就是线上那份逻辑。
    # 直接抓 grep -qE '<表达式>' 的单引号内容，不做转义改写。
    patterns = re.findall(r"grep -qE '([^']+)'", text)
    if len(patterns) < 2:
        check("能从脚本抽出正则", False,
              "只找到 %d 条：%r" % (len(patterns), patterns))
        return 1
    check("能从脚本抽出正则", True)

    re_fmt = re.compile(patterns[0])     # IPv4 格式校验
    re_priv = re.compile(patterns[1])    # 私网/保留地址段

    def is_public(ip: str) -> bool:
        if not re_fmt.search(ip):
            return False
        return not re_priv.search(ip)

    print("=== 公网地址应被接受 ===")
    for ip in ["8.152.0.186", "1.1.1.1", "114.114.114.114",
               "47.100.200.99", "120.24.1.1", "223.5.5.5"]:
        check("接受 %s" % ip, is_public(ip) is True)

    print("\n=== 内网/保留地址应被拒绝 ===")
    for ip in ["172.16.0.1", "172.20.11.5", "172.31.255.255",
               "10.0.0.1", "192.168.1.1", "127.0.0.1", "169.254.1.1",
               "0.0.0.0", "100.64.0.1", "100.127.255.255",
               "224.0.0.1", "240.0.0.1", "255.255.255.255"]:
        check("拒绝 %s" % ip, is_public(ip) is False)

    print("\n=== 非法格式应被拒绝 ===")
    for ip in ["", "abc", "1.2.3", "1.2.3.4.5", "999.999.999.999",
               "::1", "fe80::1", "-1.2.3.4", " 8.152.0.186 "]:
        check("拒绝 %r" % ip, is_public(ip) is False, "注意前后空格")

    print("\n=== 边界：172.15 和 172.32 属公网 ===")
    check("172.15.0.1 算公网", is_public("172.15.0.1") is True)
    check("172.32.0.1 算公网", is_public("172.32.0.1") is True)

    print("\n=== 脚本结构检查 ===")
    check("支持 --ip 参数", "--ip)" in text)
    # hostname -I 只允许出现在注释里，不能作为实际探测命令
    code_lines = [ln for ln in text.splitlines()
                  if "hostname -I" in ln and not ln.lstrip().startswith("#")]
    check("hostname -I 不出现在可执行代码中", not code_lines, code_lines)
    check("有多个探测源互备", "ipify" in text and "icanhazip" in text)
    check("探测失败会要求手动输入", "请输入服务器公网 IP" in text)
    check("mirror.json 每次都会刷新", "mirror.json.new" in text)
    check("刷新前有备份", "mirror.json.bak" in text)
    check("保留用户自定义项", '"keepVersions"' in text and '"repo"' in text)
    check("地址字段由 Python 强制重写", "publicBaseUrl" in text)

    total = len(PASS) + len(FAIL)
    print("\n%d/%d checks passed" % (len(PASS), total))
    if FAIL:
        print("Failed:")
        for f in FAIL:
            print("  - " + f)
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
