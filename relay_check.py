#!/usr/bin/env python3
"""relay-check — 本地自测中转站「身份 + 计费」探针工具（MVP）。

用户在本机运行本工具，填入中转站地址（base URL）与自己的 Key，
对该站声称提供的模型做少量探针请求，观察两类信号：

  1. 身份信号：返回的 model 字段、自述身份与目标模型是否一致；
  2. 计费信号：固定文本探针返回的 usage 与基线用量是否存在明显偏差。

硬性纪律（写死在本工具的行为里）：
  - Key 只从环境变量读取（--key-env 指定变量名），不打印、不写日志、
    不落盘、不进入任何报告文件。
  - 结论用语只有三种：「疑似」/「未发现异常」/「未测试」，不下定论。
  - 每次报告都带测试时间、方法说明、免责声明与已知上限。

仅使用 Python 标准库，Python 3.10+。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TOOL_VERSION = "0.1.0"

VERDICT_SUSPECT = "疑似"
VERDICT_OK = "未发现异常"
VERDICT_UNTESTED = "未测试"

# 归入「身份」检查大类的题目类别；其余 "billing" 归入「计费」大类。
IDENTITY_CATEGORIES = {"identity", "knowledge-cutoff", "format-habit", "long-context"}
BILLING_CATEGORIES = {"billing"}

DISCLAIMER = (
    "本报告由 relay-check 在用户本机自动生成，仅反映本次少量探针在测试时刻的返回情况。"
    "「疑似」表示观察到异常信号，「未发现异常」表示在本次探针范围内未观察到异常信号，"
    "二者均不构成对任何服务商的确定性结论、指控或背书。请结合多次、不同时段的测试自行判断。"
)

LIMITATIONS = [
    "无法检测 usage 字段本身造假：若服务商在返回的 usage 数字上直接编造，本工具的计费比对会被绕过。",
    "长上下文探针只做小规格：受单次运行成本约束，探针上下文长度有限，无法覆盖大上下文下的替换或截断行为。",
    "身份信号是弱证据：服务商可用系统提示词让后端模型自称任意身份，自述一致不等于后端真实，自述不一致也可能源于提示词设置。",
]

METHOD_SUMMARY = (
    "方法：对每个探针向目标接口发送一次聊天请求，记录 HTTP 状态、返回的 model 字段、"
    "回答摘录（前 200 字）、返回的 usage（输入/输出 token）与耗时；"
    "身份类探针按题目文件中的 expect 规格做关键词/家族匹配与 model 字段比对；"
    "计费类探针将返回 usage 与基线文件中同题目的中位用量对比，"
    "总量相对偏差超过阈值（默认 0.2）即标记为「疑似」。"
    "本工具不使用本地分词器，计费比对完全依赖接口返回的 usage 数字。"
)

# 自述身份的家族关键词（小写匹配，取最早出现位置所属家族）。
FAMILY_KEYWORDS = [
    ("claude", ["claude", "anthropic"]),
    ("gpt", ["chatgpt", "gpt-5", "gpt-4", "gpt", "openai"]),
    ("deepseek", ["deepseek"]),
    ("gemini", ["gemini"]),
    ("qwen", ["qwen", "通义千问", "通义"]),
    ("glm", ["glm", "智谱"]),
    ("kimi", ["kimi", "moonshot"]),
    ("llama", ["llama"]),
    ("mistral", ["mistral"]),
]


def detect_family(text):
    """从回答文本中粗略判断其自称的模型家族；判断不出返回 None。"""
    if not text:
        return None
    lowered = text.lower()
    best_family = None
    best_pos = None
    for family, keywords in FAMILY_KEYWORDS:
        for kw in keywords:
            pos = lowered.find(kw.lower())
            if pos != -1 and (best_pos is None or pos < best_pos):
                best_family = family
                best_pos = pos
    return best_family


def load_json_file(path, what):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ValueError(f"{what}文件不存在：{path}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what}文件不是合法 JSON：{path}（{exc}）")


def load_questions(path):
    data = load_json_file(path, "题目")
    probes = data.get("probes") if isinstance(data, dict) else data
    if not isinstance(probes, list) or not probes:
        raise ValueError("题目文件格式错误：应为题目数组，或含 probes 数组的对象")
    for probe in probes:
        for field in ("id", "category", "prompt"):
            if field not in probe:
                raise ValueError(f"题目缺少必填字段 {field}：{probe.get('id', '<unknown>')}")
    return probes


def load_baseline(path):
    data = load_json_file(path, "基线")
    probes = data.get("probes") if isinstance(data, dict) else None
    if not isinstance(probes, dict):
        raise ValueError("基线文件格式错误：应为含 probes 对象的 JSON")
    return probes


def build_request(fmt, base_url, model, prompt, api_key):
    """按接口格式组装请求。api_key 只进入请求头，绝不写入任何输出。"""
    base = base_url.rstrip("/")
    if fmt == "openai":
        url = base + "/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        }
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0,
        }
    elif fmt == "anthropic":
        url = base + "/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        body = {
            "model": model,
            "max_tokens": 512,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
    else:  # argparse 已限制取值，这里只是兜底
        raise ValueError(f"未知接口格式：{fmt}")
    return url, headers, json.dumps(body).encode("utf-8")


def do_request(url, headers, payload, timeout=60):
    """发送一次请求，返回 (status, parsed_json_or_None, raw_text, latency_s, error_str)。"""
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        status = exc.code
        latency = time.monotonic() - start
        return status, None, raw, latency, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        latency = time.monotonic() - start
        return None, None, "", latency, f"请求失败：{exc.__class__.__name__}"
    latency = time.monotonic() - start
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return status, None, raw, latency, "响应不是合法 JSON"
    return status, parsed, raw, latency, None


def extract(fmt, data):
    """从响应 JSON 中提取 (response_model, answer_text, usage_normalized, usage_raw)。"""
    if not isinstance(data, dict):
        return None, "", {}, {}
    response_model = data.get("model")
    usage_raw = data.get("usage") or {}
    if fmt == "openai":
        text = ""
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                text = content
        usage = {
            "prompt_tokens": usage_raw.get("prompt_tokens"),
            "completion_tokens": usage_raw.get("completion_tokens"),
        }
    else:  # anthropic
        parts = []
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        text = "".join(parts)
        usage = {
            "prompt_tokens": usage_raw.get("input_tokens"),
            "completion_tokens": usage_raw.get("output_tokens"),
        }
    total = None
    if isinstance(usage.get("prompt_tokens"), (int, float)) and isinstance(
        usage.get("completion_tokens"), (int, float)
    ):
        total = usage["prompt_tokens"] + usage["completion_tokens"]
    usage["total_tokens"] = total
    return response_model, text, usage, usage_raw


def relative_deviation(returned, baseline):
    """相对偏差；基线为 0/缺失时返回 None。"""
    if returned is None or baseline is None or baseline == 0:
        return None
    return abs(returned - baseline) / abs(baseline)


def check_probe(probe, fmt, base_url, model, api_key, baseline_entry, threshold):
    """运行单个探针并给出结论。返回值只包含可公开记录的信息（不含 Key）。"""
    url, headers, payload = build_request(fmt, base_url, model, probe["prompt"], api_key)
    status, data, _raw, latency, error = do_request(url, headers, payload)
    response_model, text, usage, usage_raw = extract(fmt, data) if data else (None, "", {}, {})
    expect = probe.get("expect") or {}
    signals = []

    result = {
        "id": probe["id"],
        "category": probe["category"],
        "http_status": status,
        "error": error,
        "response_model": response_model,
        "model_field_match": None,
        "claimed_family": detect_family(text),
        "excerpt": text[:200],
        "usage": usage,
        "usage_raw": usage_raw,
        "latency_s": round(latency, 3),
        "signals": signals,
        "billing": None,
        "verdict": VERDICT_UNTESTED,
    }

    if status != 200 or data is None:
        signals.append(f"请求未成功（{error or '无响应'}），本题未测试")
        return result

    # —— 身份类信号 ——
    if expect.get("check_model_field"):
        match = response_model == model
        result["model_field_match"] = match
        if response_model is not None and not match:
            signals.append(
                f"返回的 model 字段为「{response_model}」，与所请求的「{model}」不一致"
            )
    expected_family = expect.get("expected_family")
    if expected_family:
        claimed = result["claimed_family"]
        if claimed is not None and claimed != expected_family:
            signals.append(
                f"回答自称家族为「{claimed}」，与题目预期的「{expected_family}」不一致"
            )
    keywords = expect.get("expected_keywords_any")
    if keywords:
        if not any(kw in text for kw in keywords):
            signals.append("回答未包含题目预期的任一关键词：" + "、".join(keywords))

    verdict = VERDICT_SUSPECT if signals else VERDICT_OK

    # —— 计费类核对（固定文本探针） ——
    if probe["category"] in BILLING_CATEGORIES or probe.get("fixed_text"):
        if baseline_entry is None:
            result["billing"] = {"compared": False, "reason": "未提供该题目的基线用量"}
            verdict = VERDICT_UNTESTED
        else:
            base_usage = baseline_entry.get("usage") or {}
            dev_prompt = relative_deviation(usage.get("prompt_tokens"), base_usage.get("prompt_tokens"))
            dev_completion = relative_deviation(
                usage.get("completion_tokens"), base_usage.get("completion_tokens")
            )
            dev_total = relative_deviation(usage.get("total_tokens"), (
                (base_usage.get("prompt_tokens") or 0) + (base_usage.get("completion_tokens") or 0)
                if base_usage.get("prompt_tokens") is not None and base_usage.get("completion_tokens") is not None
                else None
            ))
            billing_info = {
                "compared": True,
                "baseline_usage": base_usage,
                "deviation_prompt": dev_prompt,
                "deviation_completion": dev_completion,
                "deviation_total": dev_total,
                "threshold": threshold,
            }
            result["billing"] = billing_info
            if dev_total is not None and dev_total > threshold:
                signals.append(
                    f"usage 总量相对基线偏差 {dev_total:.1%}，超过阈值 {threshold:.0%}"
                )
                verdict = VERDICT_SUSPECT
            elif dev_total is None:
                verdict = VERDICT_UNTESTED
                signals.append("基线或返回 usage 不完整，无法比对")
            else:
                verdict = VERDICT_OK

    result["verdict"] = verdict
    return result


def aggregate_verdict(results):
    """一组探针结果 → 一个结论。用语只有三种。"""
    if not results:
        return VERDICT_UNTESTED
    verdicts = [r["verdict"] for r in results]
    if any(v == VERDICT_SUSPECT for v in verdicts):
        return VERDICT_SUSPECT
    if all(v == VERDICT_UNTESTED for v in verdicts):
        return VERDICT_UNTESTED
    return VERDICT_OK


def write_reports(report, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "report.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    lines = []
    lines.append("# relay-check 测试报告")
    lines.append("")
    lines.append(f"- 测试时间：{report['timestamp']}")
    lines.append(f"- 目标地址：{report['target']['base_url']}")
    lines.append(f"- 声称模型：{report['target']['model']}")
    lines.append(f"- 接口格式：{report['target']['format']}")
    lines.append(f"- 计费偏差阈值：{report['threshold']}")
    lines.append(f"- 基线：{'已使用' if report['baseline_used'] else '未提供（计费类结论为未测试）'}")
    lines.append("")
    lines.append("## 方法")
    lines.append("")
    lines.append(report["method"])
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 身份类：**{report['checks']['identity']}**")
    lines.append(f"- 计费类：**{report['checks']['billing']}**")
    lines.append(f"- 总体：**{report['overall_verdict']}**")
    lines.append("")
    lines.append("## 逐题结果")
    lines.append("")
    lines.append("| 题目 | 类别 | HTTP | 返回 model | 用量（入/出） | 结论 | 信号 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in report["probes"]:
        usage = r["usage"]
        usage_str = f"{usage.get('prompt_tokens')}/{usage.get('completion_tokens')}"
        signal_str = "；".join(r["signals"]) if r["signals"] else "—"
        lines.append(
            f"| {r['id']} | {r['category']} | {r['http_status']} | "
            f"{r['response_model'] or '—'} | {usage_str} | {r['verdict']} | {signal_str} |"
        )
    lines.append("")
    lines.append("## 免责声明")
    lines.append("")
    lines.append(report["disclaimer"])
    lines.append("")
    lines.append("## 已知上限")
    lines.append("")
    for item in report["limitations"]:
        lines.append(f"- {item}")
    lines.append("")
    md_path = os.path.join(out_dir, "report.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return json_path, md_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="relay-check",
        description="本机自测中转站的身份与计费信号（结论用语仅限：疑似 / 未发现异常 / 未测试）。",
    )
    parser.add_argument("--base-url", required=True, help="中转站地址，如 https://example.com")
    parser.add_argument("--model", required=True, help="要测试的声称模型名，如 claude-sonnet-4-5")
    parser.add_argument("--format", choices=["openai", "anthropic"], default="openai",
                        help="接口格式（默认 openai）")
    parser.add_argument("--key-env", default="RELAY_CHECK_API_KEY",
                        help="存放 Key 的环境变量名（默认 RELAY_CHECK_API_KEY）；Key 只从环境变量读取")
    parser.add_argument("--questions", required=True, help="题目 JSON 文件路径")
    parser.add_argument("--baseline", default=None, help="基线 JSON 文件路径（可选；无基线时计费类结论为未测试）")
    parser.add_argument("--out", default="relay-check-out", help="报告输出目录（默认 relay-check-out）")
    parser.add_argument("--threshold", type=float, default=0.2,
                        help="计费偏差阈值，相对偏差超过即标记疑似（默认 0.2）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验配置并打印脱敏计划，不发出任何网络请求")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    try:
        probes = load_questions(args.questions)
        baseline_probes = load_baseline(args.baseline) if args.baseline else {}
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    api_key = os.environ.get(args.key_env)
    key_is_set = bool(api_key)

    if args.dry_run:
        plan = {
            "dry_run": True,
            "base_url": args.base_url,
            "model": args.model,
            "format": args.format,
            "key_env": args.key_env,
            "key_is_set": key_is_set,  # 只报是否已设置，绝不输出 Key 本身
            "questions_file": args.questions,
            "probe_count": len(probes),
            "probe_ids": [p["id"] for p in probes],
            "baseline_file": args.baseline,
            "baseline_probe_ids": sorted(baseline_probes.keys()),
            "threshold": args.threshold,
            "out_dir": args.out,
            "network_requests_planned": len(probes),
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if not key_is_set:
            print(f"提示：环境变量 {args.key_env} 未设置，正式运行前请先设置。", file=sys.stderr)
        return 0

    if not key_is_set:
        print(f"错误：环境变量 {args.key_env} 未设置或为空；Key 只能通过环境变量提供。", file=sys.stderr)
        return 2

    results = []
    for probe in probes:
        baseline_entry = baseline_probes.get(probe["id"])
        results.append(
            check_probe(probe, args.format, args.base_url, args.model, api_key, baseline_entry, args.threshold)
        )

    categories = {}
    for probe in probes:
        cat = probe["category"]
        categories[cat] = aggregate_verdict([r for r in results if r["category"] == cat])

    identity_results = [r for r in results if r["category"] in IDENTITY_CATEGORIES]
    billing_results = [r for r in results if r["category"] in BILLING_CATEGORIES]
    checks = {
        "identity": aggregate_verdict(identity_results),
        "billing": aggregate_verdict(billing_results),
    }
    overall = aggregate_verdict(
        [{"verdict": checks["identity"]}, {"verdict": checks["billing"]}]
    )

    report = {
        "tool": "relay-check",
        "version": TOOL_VERSION,
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "target": {
            "base_url": args.base_url,
            "model": args.model,
            "format": args.format,
        },
        "method": METHOD_SUMMARY,
        "threshold": args.threshold,
        "baseline_used": bool(args.baseline),
        "probes": results,
        "categories": categories,
        "checks": checks,
        "overall_verdict": overall,
        "disclaimer": DISCLAIMER,
        "limitations": LIMITATIONS,
    }

    json_path, md_path = write_reports(report, args.out)
    print(f"身份类：{checks['identity']}｜计费类：{checks['billing']}｜总体：{overall}")
    print(f"报告已写入：{json_path}、{md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
