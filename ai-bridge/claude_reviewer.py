# -*- coding: utf-8 -*-
"""
Claude 审查桥接脚本（优先通道）
通过本机 claude-console 服务（http://127.0.0.1:4173/api/chat）驱动本机 Claude Code CLI，
对项目代码 / 设计方案进行审查，拿回审查意见供主流程修改整合。
若 claude-console 服务未启动，自动降级为直接调用本机 claude CLI。

用法：
    python claude_reviewer.py --target code   --files backend/src/.../Xxx.java,frontend/src/.../Xxx.vue
    python claude_reviewer.py --target design --text  docs/设计方案正文.md
参数：
    --target  审查类型：code（代码审查）/ design（设计方案评审）
    --files   待审查文件路径，逗号分隔（多个文件会自动拼接，受 8000 字符上限约束）
    --text    直接传入待审查文本或文本文件路径
    --out     审查报告输出文件（可选，默认打印）
    --cwd     给 Claude 的工作目录（默认项目根目录）
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

# Windows 控制台兼容：所有输出统一按 UTF-8 编码，避免中文报错
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = r"C:\Users\Administrator\Desktop\设计\自习室预约管理系统"
CONSOLE_API = "http://127.0.0.1:4173/api/chat"
MAX_PROMPT_LEN = 8000  # claude-console 单条 prompt 上限

SYSTEM_HEAD = {
    "code": (
        "你是一位资深全栈代码审查员，精通 Spring Boot 3 + Vue 3 + Python(OpenCV)。"
        "请审查下面代码，输出中文审查报告，按严重程度分级，每条给出【问题描述 -> 所在文件/位置 -> 具体修改建议】：\n"
        "1) 致命问题：编译错误、安全漏洞（SQL注入/XSS/越权/明文密码等）\n"
        "2) 功能缺陷：并发冲突、边界条件、空指针、异常处理缺失\n"
        "3) 规范问题：命名、分层、硬编码、重复代码\n"
        "4) 架构建议：模块划分、接口设计、可扩展性\n"
        "重要约束：你只负责审查并输出意见，绝对禁止修改任何文件、禁止执行任何写操作。"
    ),
    "design": (
        "你是一位高校信息系统架构评审专家。请评审下面的系统设计方案，输出中文评审报告：\n"
        "1) 需求覆盖度：功能是否完整、预约业务流程是否闭环\n"
        "2) 架构合理性：前后端交互、视觉识别(座位占用检测)集成方式、扩展性\n"
        "3) 数据库设计：表结构、字段类型、索引、外键关系是否合理\n"
        "4) 风险与盲点：并发预约抢座、时间冲突、占座逃课、摄像头场景限制、隐私合规\n"
        "5) 改进建议清单（具体可落地，不要空泛）\n"
        "重要约束：你只负责评审并输出意见，绝对禁止修改任何文件。"
    ),
}


def read_materials(files, text):
    parts = []
    if text:
        if os.path.isfile(text):
            with open(text, "r", encoding="utf-8", errors="replace") as f:
                parts.append(f.read())
        else:
            parts.append(text)
    for fp in files or []:
        fp = fp.strip()
        if not fp:
            continue
        if not os.path.isfile(fp):
            sys.exit("错误：文件不存在 -> %s" % fp)
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        parts.append("===== 文件: %s =====\n%s" % (os.path.basename(fp), content))
    return "\n\n".join(parts)


def build_prompt(target, materials):
    prompt = SYSTEM_HEAD[target] + "\n\n===== 待审查内容 =====\n" + materials
    if len(prompt) > MAX_PROMPT_LEN:
        prompt = prompt[:MAX_PROMPT_LEN] + "\n\n（注：内容过长已截断，仅审查前 %d 字符）" % MAX_PROMPT_LEN
    return prompt


def call_via_console(prompt, cwd):
    """通过 claude-console 的 HTTP 接口调用（SSE 流式）。收集 result 事件与裸文本行。"""
    payload = {
        "prompt": prompt,
        "cwd": cwd,
        "maxTotalLen": MAX_PROMPT_LEN,
    }
    req = urllib.request.Request(
        CONSOLE_API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    results = []
    errors = []
    with urllib.request.urlopen(req, timeout=320) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            try:
                evt = json.loads(data)
            except Exception:
                # 非 JSON 裸行：deepseek 模型经 claude-console 转发时部分输出不带 envelope，
                # 这些行本身就是 Claude 的回答文本，直接收集。
                if data and not data.startswith("{"):
                    results.append(data)
                continue
            if evt.get("type") == "result" and isinstance(evt.get("result"), str):
                results.append(evt["result"])
            elif evt.get("type") == "error":
                msg = str(evt.get("message", ""))
                # 形如“无法解析的流输出: xxx”的裸行同样属于 Claude 输出，尽量还原内容
                if msg.startswith("无法解析的流输出:"):
                    results.append(msg[len("无法解析的流输出:"):].strip())
                else:
                    errors.append(msg)
            elif evt.get("type") == "done":
                break
    text = "\n".join(x for x in results if x.strip())
    if not text.strip():
        detail = "; ".join(errors[-3:]) if errors else "无有效输出"
        raise RuntimeError("Claude 未返回审查结果：%s" % detail)
    return text


def call_via_cli(prompt, cwd):
    """降级通道：直接调用本机 claude CLI（.cmd），prompt 经 stdin 传入，纯文本输出。"""
    cmd = ["cmd", "/c", "claude", "-p", "--output-format", "text",
           "--allowedTools", "Read,Grep,Glob"]
    proc = subprocess.run(
        cmd, cwd=cwd, input=prompt, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=320,
    )
    if proc.returncode != 0:
        raise RuntimeError("claude CLI 失败(code=%s): %s" % (proc.returncode, proc.stderr[-500:]))
    return proc.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description="Claude 审查桥接")
    parser.add_argument("--target", required=True, choices=["code", "design"])
    parser.add_argument("--files", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--cwd", default=PROJECT_ROOT)
    args = parser.parse_args()

    materials = read_materials(args.files.split(",") if args.files else [], args.text)
    if not materials.strip():
        sys.exit("错误：未提供任何待审查内容。")

    prompt = build_prompt(args.target, materials)
    print("待审查材料 %d 字符，正在发送给本机 Claude 审查..." % len(materials))

    try:
        report = call_via_console(prompt, args.cwd)
        print("通道：claude-console (http://127.0.0.1:4173)")
    except Exception as e:
        print("claude-console 通道失败（%s），降级为直接调用 claude CLI..." % e)
        report = call_via_cli(prompt, args.cwd)
        print("通道：claude CLI (print 模式)")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print("审查报告已写入: %s" % args.out)
    else:
        print("\n" + "=" * 30 + " Claude 审查报告 " + "=" * 30)
        print(report)


if __name__ == "__main__":
    main()
