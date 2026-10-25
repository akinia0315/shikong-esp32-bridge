#!/usr/bin/env python3
"""把被动扫描抄到的帧按时间排开，标出变化的字节，并检查校验。

用法: python3 tools/decode_frames.py scan.json
输入是 GET /api/toy/scan 的返回（或 samples/ 里的文件），只看 17 00 71 结尾的帧。
校验规则：byte0 = byte1..byte15 求和取低 8 位；不满足的那一列会标出来，说明这类帧另有规则。
"""
import json
import sys

TAIL = "170071"


def rows(path):
    d = json.load(open(path))
    out = []
    for f in d.get("frames") or []:
        u = f.get("uuid128") or ""
        if len(u) != 32 or not u.endswith(TAIL):
            continue
        out.append((f["t0"], f["t1"], f["n"], f.get("addr", ""), bytes.fromhex(u)))
    out.sort()
    return out


def main(path):
    data = rows(path)
    if not data:
        print("没有 17 00 71 结尾的帧")
        return
    prev = None
    print("    t0     t1   n  | " + " ".join("%02d" % i for i in range(16)) + " | 变化                会话号  掩码  校验")
    for t0, t1, n, addr, fr in data:
        marks = []
        for i, b in enumerate(fr):
            if prev is None or prev[i] == b:
                marks.append("%02x" % b)
            else:
                marks.append("\033[1;33m%02x\033[0m" % b)
        diff = "" if prev is None else ",".join(
            "b%d" % i for i in range(16) if prev[i] != fr[i])
        ok = fr[0] == sum(fr[1:]) & 0xFF
        print("%6d %6d %3d | %s | %-18s  %02x%02x    %02x    %s" % (
            t0, t1, n, " ".join(marks), diff, fr[8], fr[9], fr[11], "ok" if ok else "\033[1;31m不符\033[0m"))
        prev = fr
    print()
    sessions = sorted({"%02x%02x" % (fr[8], fr[9]) for *_, fr in data})
    print("会话号（byte8-9）：", ", ".join(sessions), "← 和 shikong/proto.py 的 SESSION_ID 不一样就改那里")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "scan.json")
