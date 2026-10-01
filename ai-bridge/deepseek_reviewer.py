# -*- coding: utf-8 -*-
"""
DeepSeek 审查桥接脚本
用途：把本项目的代码/方案文档发送给 DeepSeek API 做审查（代码审查 / 架构评审 / 方案挑刺），
      拿回审查意见供主流程修改整合，实现“外部 AI 取长补短”。
用法：
    python deepseek_reviewer.py --target code  --files backend/src/main/java/.../Xxx.java
    python deepseek_reviewer.py --target design --text "完整设计方案文本或文件路径"
参数：
    --target  审查类型：code（代码审查）/ design（设计方案评审）
    --files   待审查文件路径，可多个，用逗号分隔
    --text    直接传入待审查文本（与 --files 二选一）
    --out     审查报告输出文件（可选，默认打印到 stdout）
首次使用前：把 api key 填入同目录 config.json（复制自 config.example.json）。
"""
import argparse
import json
import os
import sys
import urllib.request

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

SYSTEM_PROMPTS = {
    "code": (
        "你是一位资深全栈代码审查员，擅长 Spring Boot + Vue + Python 项目。"
        "请从以下维度审查给出的代码并输出中文审查报告：\n"
        "1) 致命问题（编译错误、逻辑漏洞、安全漏洞如SQL注入/XSS/越权）\n"
        "2) 功能缺陷（边界条件、并发、空指针、异常处理）\n"
        "3) 规范与可维护性（命名、分层、重复代码、硬编码）\n"
        "4) 针对问题的具体修改建议（给出可落地的修改方案）\n"
        "报告格式：按严重程度分级列出【问题描述 -> 所在位置 -> 修改建议】。"
    ),
    "design": (
        "你是一位高校信息系统架构评审专家。请从以下维度审查给出的系统设计方案并输出中文评审报告：\n"
        "1) 需求覆盖度（功能是否完整、业务流程是否闭环）\n"
        "2) 架构合理性（前后端交互、视觉识别集成、扩展性）\n"
        "3) 数据库设计（表结构、字段、索引、关系是否合理）\n"
        "4) 风险与盲点（并发预约、占座、时间冲突、摄像头场景限制）\n"
        "5) 给出改进建议清单。报告要求具体、可落地，不要空泛评价。"
    ),
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("错误：未找到 config.json，请先复制 config.example.json 为 config.json 并填入 API Key。")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    key = (cfg.get("api_key") or "").strip()
    if not key or key.startswith("sk-请填入"):
        sys.exit("错误：config.json 中的 api_key 为空或未填写，请填入你的 DeepSeek API Key 后重试。")
    return cfg


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


def call_deepseek(cfg, system_prompt, user_content):
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": cfg.get("max_tokens", 4000),
        "temperature": 0.3,
        "stream": False,
    }
    req = urllib.request.Request(
        cfg["base_url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % cfg["api_key"],
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def main():
    parser = argparse.ArgumentParser(description="DeepSeek 审查桥接")
    parser.add_argument("--target", required=True, choices=["code", "design"], help="审查类型")
    parser.add_argument("--files", default="", help="待审查文件，逗号分隔")
    parser.add_argument("--text", default="", help="直接传入待审查文本")
    parser.add_argument("--out", default="", help="审查报告输出文件")
    args = parser.parse_args()

    cfg = load_config()
    materials = read_materials(args.files.split(",") if args.files else [], args.text)
    if not materials.strip():
        sys.exit("错误：未提供任何待审查内容（--files / --text 至少一个）。")

    print("已读取材料 %d 字符，正在发送给 DeepSeek(%s) 审查..." % (len(materials), cfg["model"]))
    report = call_deepseek(cfg, SYSTEM_PROMPTS[args.target], materials)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print("审查报告已写入: %s" % args.out)
    else:
        print("\n" + "=" * 30 + " DeepSeek 审查报告 " + "=" * 30)
        print(report)


if __name__ == "__main__":
    main()
